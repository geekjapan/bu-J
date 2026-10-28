import base64
import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from decision_browser.web.tasks import allowed_action, filter_page, get_task, validate_url, verify_task


def test_fixed_tasks_and_date():
    task = get_task("flights")
    assert task.flight_date == "2027-09-20"
    assert "September 20, 2027" in task.goal
    with pytest.raises(FrozenInstanceError):
        task.url = "https://example.com"
    with pytest.raises(ValueError):
        get_task("arbitrary")
    with pytest.raises(ValueError):
        get_task("flights", "invalid")


@pytest.mark.parametrize("name,url", [
    ("flights", "https://www.google.com/travel/flights/booking"),
    ("flights", "https://www.google.com.evil.test/travel/flights"),
    ("flights", "https://user@www.google.com/travel/flights"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Special:UserLogin"),
    ("wikipedia", "https://en.wikipedia.org/w/index.php?title=Main_Page&action=edit"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Main_Page?veaction=edit"),
    ("hotel", "http://127.0.0.1:8766/fixture.html?scenario=research"),
    ("hotel", "http://127.0.0.1:8766/fixture.html?scenario=travel#reading"),
])
def test_scope_rejects_unapproved_urls(name, url):
    with pytest.raises(ValueError):
        validate_url(get_task(name), url)


def test_hotel_positive_controls_and_unknown_denial():
    task = get_task("hotel")
    fill = {"node": 1, "kind": "fill", "label": "Destination"}
    metadata = {"1": {"tag": "INPUT", "input_type": "search", "attributes": {"id": "destination"}}}
    assert allowed_action(task, fill, metadata, task.url)
    assert not allowed_action(task, fill, {}, task.url)
    assert not allowed_action(task, fill, {"1": {**metadata["1"], "form_action": "https://evil.test"}}, task.url)
    select = {"node": 2, "kind": "select", "label": "Stay category → Design", "value": "Design"}
    meta = {"tag": "select", "attributes": {"id": "category"}}
    assert allowed_action(task, select, meta, task.url)
    assert not allowed_action(task, {**select, "value": "Nature"}, meta, task.url)
    view = {"kind": "click", "label": "View Casa Flora"}
    assert allowed_action(task, view, {"tag": "button", "attributes": {"data-place": "Casa Flora"}}, task.url)
    assert not allowed_action(task, {**view, "label": "Book Casa Flora"}, {"tag": "button"}, task.url)


def test_wikipedia_search_and_read_links_only():
    task = get_task("wikipedia")
    action = {"kind": "click", "label": "Gödel"}
    assert allowed_action(task, action, {"tag": "a", "href": "/wiki/G%C3%B6del%27s_incompleteness_theorems"}, task.url)
    for href in ["https://example.com", "/wiki/Special:UserLogin", "/w/index.php?action=edit"]:
        assert not allowed_action(task, action, {"tag": "a", "href": href}, task.url)
    assert not allowed_action(task, {"kind": "fill", "label": "Search"}, {"tag": "input", "input_type": "password", "attributes": {"name": "search"}}, task.url)


def test_flights_search_controls_but_no_flight_selection():
    task = get_task("flights")
    for label, role in [("Where from?", "combobox"), ("London (LON)", "option"), ("Monday, September 20, 2027", "button"), ("One way", "option")]:
        assert allowed_action(task, {"kind": "click", "label": label, "role": role}, {"tag": "div"}, task.url)
    for label in ["Select flight", "Search account", "Unknown control", "Book", "Log in"]:
        assert not allowed_action(task, {"kind": "click", "label": label}, {"tag": "button"}, task.url)
    assert not allowed_action(task, {"kind": "click", "label": "Search"}, {"tag": "button", "form_method": "post"}, task.url)


def test_filter_preserves_raw_page_and_fields():
    task = get_task("hotel")
    page = {"url": task.url, "marker": [1], "actions": [{"id": "wait", "kind": "wait"}, {"kind": "click", "label": "unknown"}]}
    filtered = filter_page(task, page)
    assert len(filtered["actions"]) == 1
    assert len(page["actions"]) == 2
    assert filtered["marker"] == page["marker"]


@pytest.mark.parametrize('label', ['Next', 'Previous', 'Done. '])
def test_observed_flights_calendar_buttons_require_calendar_context(label):
    task = get_task('flights')
    action = {'kind': 'click', 'label': label, 'role': 'button'}
    metadata = {'tag': 'button', 'label': label,
        'context': 'One way Reset October November Underlined prices indicate cheapest compared with other prices shown Done'}
    assert allowed_action(task, action, metadata, task.url)
    assert not allowed_action(task, action, {**metadata, 'context': 'Next booking step'}, task.url)
    assert not allowed_action(task, action, {**metadata, 'tag': 'a'}, task.url)


def flight_page():
    encoded = base64.urlsafe_b64encode(b"departing 2027-09-20").decode().rstrip("=")
    values = {"Change ticket type. One way": "One way", "Where from?": "Zürich", "Where to?": "London", "Departure": "Mon, Sep 20", "1 passenger": "", "Economy": "Economy"}
    return {"url": f"https://www.google.com/travel/flights/search?tfs={encoded}", "text": "", "actions": [{"label": label, "value": value} for label, value in values.items()] + [{"label": "Leaves Zurich Airport on Monday, September 20 and arrives London. Select flight"}]}


def test_flights_independent_verifier_requires_matching_results_and_supplemental_settings():
    task = get_task("flights")
    page = flight_page()
    assert verify_task(task, page)["passed"]
    for label in ["1 passenger", "Economy", "Departure"]:
        bad = {**page, "actions": [a for a in page["actions"] if a["label"] != label], "status": "done"}
        assert not verify_task(task, bad)["passed"]
    page["actions"][-1]["label"] = "Sunday, September 20. Select flight"
    assert not verify_task(task, page)["passed"]


def test_hotel_and_wikipedia_independent_verifiers():
    hotel = get_task("hotel")
    page = {"url": hotel.url + "#casa-flora", "text": "Casa Flora\nFree cancellation included\nYour filters: Design · Free cancellation enabled · Destination Lisbon"}
    assert verify_task(hotel, page)["passed"]
    assert not verify_task(hotel, {**page, "text": "Casa Flora", "status": "done"})["passed"]
    wiki = get_task("wikipedia")
    target = "https://en.wikipedia.org/wiki/G%C3%B6del%27s_incompleteness_theorems"
    assert not verify_task(wiki, {"url": target})["passed"]
    assert not verify_task(wiki, {"url": target, "title": "Wikimedia Error", "text": "HTTP 429"})["passed"]
    assert verify_task(wiki, {"url": target, "title": "Gödel's incompleteness theorems - Wikipedia",
        "text": "Gödel's incompleteness theorems are results of mathematical logic. " * 5})["passed"]
    assert not verify_task(wiki, {"url": target + "?action=edit", "status": "done"})["passed"]


def test_fixture_matches_pinned_source_and_recorded_hash():
    root = Path(__file__).resolve().parents[1]
    folder = root / "fixtures/web_benchmark"
    provenance = json.loads((folder / "UPSTREAM.json").read_text())
    fixture = (folder / "fixture.html").read_bytes()
    assert hashlib.sha256(fixture).hexdigest() == provenance["sha256"]
    assert "Copyright (c) 2026 Browser Use" in provenance["license_text"]


def test_observed_google_control_labels_and_fixture_submit_scope():
    flights=get_task('flights')
    for label,role in [('1 passenger, change number of passengers.','button'),('Change seating class. Economy','combobox')]:
        action={'id':'control','kind':'click','label':label,'node':1,'role':role}
        meta={'1':{'tag':'div','attributes':{'role':role}}}
        assert allowed_action(flights,action,meta,flights.url)
    hotel=get_task('hotel')
    action={'id':'find','kind':'click','label':'Find stays','node':7}
    meta={'7':{'tag':'button','input_type':'submit','attributes':{},'form_id':'search',
        'form_method':'get','form_action':hotel.url,'context':'Find stays'}}
    assert allowed_action(hotel,action,meta,hotel.url)
    assert not allowed_action(hotel,action,{'7':{**meta['7'],'form_id':'unknown'}},hotel.url)


def test_flights_types_only_into_observed_autocomplete_editor():
    task=get_task('flights')
    a={'kind':'fill','label':'Where from?','role':'combobox'}
    meta={'tag':'input','context':'Tokyo'}
    assert not allowed_action(task,a,meta,task.url)
    editor={**meta,'context':'Tokyo Remove Select multiple airports Done'}
    assert allowed_action(task,{**a,'label':'Where else?'},editor,task.url)
    assert allowed_action(task,{**a,'label':'Where to?'},editor,task.url)
    assert not allowed_action(task,{**a,'label':'Departure'},editor,task.url)


def test_calendar_only_exposes_authorized_departure_date():
    task=get_task('flights')
    for label,expected in [('Monday, September 20, 2027',True),('Sunday, September 19, 2027',False),('Sunday, September 20, 2026',False)]:
        assert allowed_action(task,{'kind':'click','label':label,'role':'button'},{'tag':'div'},task.url)==expected


def test_city_search_excludes_other_cities_and_individual_airports():
    task=get_task('flights')
    for name in ['Lake Zurich, Illinois, USA','Zurich, Netherlands','Zurich Airport (ZRH)','London Stansted Airport (STN)']:
        assert not allowed_action(task,{'kind':'click','label':name,'role':'option'},{'tag':'li'},task.url)


def test_calendar_does_not_offer_background_page_scroll():
    task=get_task('flights');scroll={'kind':'scroll','id':'scroll_down','label':'Scroll down'}
    next_button={'kind':'click','label':'Next','node':1}
    meta={'1':{'tag':'button','label':'Next','context':'Underlined prices indicate cheapest compared with other prices shown'}}
    p={'url':task.url,'actions':[scroll,next_button]}
    assert filter_page(task,p,meta)['actions']==[next_button]
    assert filter_page(task,{**p,'actions':[scroll]},meta)['actions']==[scroll]
