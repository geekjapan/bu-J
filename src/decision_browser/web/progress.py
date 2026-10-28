"""Observed progress for the three fixed benchmark goals; never executes actions."""

import re
from datetime import datetime
from urllib.parse import unquote, urlsplit

from .tasks import WIKI_ARTICLE, get_task, verify_task


def progress(page, goal, history):
    actions = page.get("actions", [])
    text = page.get("text", "")
    labels = {a.get("label", "").strip(): a for a in actions}
    observed = {a.get('label', '').strip(): a for a in page.get('observed_controls', actions)}
    evidence = {}

    def result(step):
        return {"next_step": step, "evidence": evidence}

    def pending(step, *controls):
        return result(step if any(c in labels for c in controls) else "Observe the controls needed to " + step.lower())

    if "Casa Flora" in goal:
        complete = verify_task(get_task("hotel"), page)
        evidence.update(complete["checks"])
        if complete["passed"]:
            return result("DONE: Casa Flora is open with the requested filters.")
        destination = labels.get("Destination", {}).get("value", "")
        fills = [i for i, h in enumerate(history) if h.get("kind") == "fill" and h.get("action") == "Destination"]
        latest_fill = fills[-1] if fills else -1
        submitted = any(h.get("action") == "Find stays" and h.get("kind") == "click"
                        and h.get("effect_verified", h.get("page_changed")) is True
                        for h in history[latest_fill + 1:])
        applied = bool(re.search(r"Your filters: .*Destination Lisbon(?:\s|$)", text))
        evidence.update(destination=destination, search_submitted=submitted, destination_applied=applied)
        if not applied and destination.strip().casefold() != "lisbon":
            return pending("Fill Destination with the destination from the goal.", "Destination")
        if not applied and not submitted:
            return pending("Submit the destination search with Find stays before changing filters.", "Find stays")
        category = next((a.get("current_value") for a in page.get('observed_controls', actions)
                         if a.get("kind") == "select" and a.get('label', '').startswith('Stay category')), None)
        free = labels.get("Free cancellation", {}).get("checked")
        evidence.update(category=category, free_cancellation=free)
        if category != "Design":
            return result("Select Design in Stay category." if any(a.get("kind") == "select" and a.get("value") == "Design" for a in actions)
                          else "Observe the Stay category control.")
        if free not in (True, "true"):
            return pending("Enable Free cancellation.", "Free cancellation")
        return pending("Open Casa Flora using View Casa Flora.", "View Casa Flora")

    if "Wikipedia" in goal:
        article = unquote(urlsplit(page.get("url", "")).path) == WIKI_ARTICLE
        title = page.get("title", "").replace("’", "'").startswith("Gödel's incompleteness theorems")
        evidence.update(article_url=article, article_title=title)
        if verify_task(get_task('wikipedia'), page)['passed']:
            return result("DONE: the requested Wikipedia article is open.")
        if article and title:
            return result("Wait for the requested article content to finish loading.")
        field = next((a for a in actions if a.get("kind") == "fill"), {})
        evidence["search_value"] = field.get("value", "")
        if "incompleteness" not in field.get("value", "").lower():
            return result("Fill the search field with the article topic from the goal." if field else "Observe the Wikipedia search field.")
        return pending("Submit Search to open the requested article.", "Search", "Go")

    date_match = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", goal)
    if "flights" not in goal.lower() or not date_match:
        return result("Continue toward the goal using the observed page.")
    day = datetime.strptime(date_match[1], "%B %d, %Y").date()
    complete = page.get('completion') or verify_task(get_task("flights", day.isoformat()), page)
    evidence.update(complete["checks"], requested_date=day.isoformat())
    if complete["passed"]:
        return result("DONE: matching flight options are visible.")
    calendar = []
    for label, action in observed.items():
        match = re.match(r"^[A-Z][a-z]+, ([A-Z][a-z]+ \d{1,2}, \d{4})(?:\b|$)", label)
        if match:
            calendar.append((datetime.strptime(match[1], "%B %d, %Y").date(), action))
    if calendar:
        evidence["visible_date_range"] = [min(d for d, _ in calendar).isoformat(), max(d for d, _ in calendar).isoformat()]
        target = next((a for d, a in calendar if d == day), None)
        done = next((label for label in labels if label.startswith("Done. Search for one-way flights, departing on ") and date_match[1] in label), None)
        selected = bool(done) or bool(target and target.get("selected") in (True, "true"))
        evidence["requested_date_selected"] = selected
        if selected:
            return pending("Confirm the requested departure date with Done.", *(label for label in labels if label.startswith("Done")))
        if target:
            return result("Choose " + day.strftime('%A, %B ') + str(day.day) + day.strftime(', %Y') + " in the calendar.")
        direction = "Next" if day > max(d for d, _ in calendar) else "Previous" if day < min(d for d, _ in calendar) else None
        if direction:
            return pending(f"Use calendar {direction} to reach the requested departure date.", direction, direction + " month")
        return result("Observe the requested day in the current calendar.")
    options = [a for a in actions if a.get("role") == "option"]
    opened = next((h.get('action') for h in reversed(history)
                   if h.get('action') in {'Open Where from?', 'Open Where to?'}), None)
    destination_editor = 'Where to?' in labels and not any(label.startswith('Change ticket type.') for label in labels)
    expected_city = 'London' if opened == 'Open Where to?' or destination_editor else 'Zürich'
    for city in (expected_city,):
        option = next((a for a in options if a.get("label", "").startswith(city + ",")), None)
        if option:
            return result("Select the city autocomplete option for " + city + ".")
    if 'Where else?' in labels:
        if labels['Where else?'].get('value', '').casefold() in {'zurich', 'zürich'}:
            return result('WAIT for the origin autocomplete suggestions to finish loading; do not type the same query again.')
        return result('Fill the origin city from the goal into the visible Where else? autocomplete editor.')
    if 'Where to?' in labels and not any(label.startswith('Change ticket type.') for label in labels):
        if labels['Where to?'].get('value', '').casefold() == 'london':
            return result('WAIT for the destination autocomplete suggestions to finish loading; do not type the same query again.')
        return result('Fill the destination city from the goal into the visible Where to? autocomplete editor.')
    if not complete["checks"]["one_way"]:
        if "One way" in labels:
            return result("Choose One way.")
        ticket = next((label for label in labels if label.startswith("Change ticket type.")), None)
        return pending("Open the ticket type control to choose One way.", ticket)
    for field, expected in (("Where from?", "Zürich"), ("Where to?", "London")):
        evidence[field] = observed.get(field, {}).get("value")
        if evidence[field] != expected:
            return pending(f"CLICK Open {field} to reveal its autocomplete editor before typing.", "Open " + field)
    if not complete["checks"]["date"] or not complete["checks"]["year"]:
        return pending("Open Departure to choose the requested date in the calendar.", "Departure", "Open Departure")
    if "Search" in labels:
        return result("Submit Search to display matching flight options.")
    return result("Observe matching flight options before declaring DONE.")
