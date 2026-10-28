import base64
import binascii
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, unquote, urljoin, urlsplit


@dataclass(frozen=True)
class WebTask:
    name: str
    url: str
    goal: str
    flight_date: str
    allowed_origins: tuple[str, ...]


WIKI_ARTICLE = "/wiki/Gödel's_incompleteness_theorems"


def get_task(name: str, flight_date: str = "2027-09-20") -> WebTask:
    day = date.fromisoformat(flight_date)
    tasks = {
        "flights": (
            "https://www.google.com/travel/flights?hl=en",
            f"Find one-way flights from Zurich to London on {day.strftime('%B')} {day.day}, {day.year}, "
            "for one adult in economy. Stop when matching flight options are visible. Do not select or book a flight.",
            ("https://www.google.com",),
        ),
        "wikipedia": (
            "https://en.wikipedia.org/wiki/Main_Page",
            "Find and open the Wikipedia article about Gödel’s incompleteness theorems.",
            ("https://en.wikipedia.org",),
        ),
        "hotel": (
            "http://127.0.0.1:8766/fixture.html?scenario=travel",
            "Use the destination search and filters to find Design stays in Lisbon with Free cancellation, then open Casa Flora.",
            ("http://127.0.0.1:8766",),
        ),
    }
    if name not in tasks:
        raise ValueError(f"Unknown benchmark task: {name}")
    url, goal, origins = tasks[name]
    return WebTask(name, url, goal, day.isoformat(), origins)


def validate_url(task: WebTask, url: str) -> None:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.username or parsed.password or origin not in task.allowed_origins:
        raise ValueError("Unapproved benchmark origin")
    path = unquote(parsed.path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if task.name == "flights":
        permitted = path in {"/travel/flights", "/travel/flights/search"}
    elif task.name == "wikipedia":
        permitted = (path.startswith("/wiki/") and ":" not in path[6:]) or path == "/w/index.php"
        permitted = permitted and all(key in {"search", "title", "fulltext", "ns0", "go", "limit", "offset"} for key in query)
        if "title" in query:
            permitted = permitted and all(":" not in title or title == "Special:Search" for title in query["title"])
    elif task.name == "hotel":
        permitted = path == "/fixture.html" and query == {"scenario": ["travel"]}
        permitted = permitted and parsed.fragment in {"", "casa-flora"}
    else:
        permitted = False
    if not permitted or "\\" in url or any(ord(c) < 32 for c in url):
        raise ValueError("Unapproved benchmark path")


def allowed_action(task: WebTask, action: dict, metadata: dict, url: str) -> bool:
    try:
        validate_url(task, url)
    except (ValueError, TypeError):
        return False
    kind = action.get("kind")
    if kind == "wait":
        return action.get("id") == "wait"
    if kind == "scroll":
        return action.get("id") in {"scroll_up", "scroll_down"}
    meta = metadata.get(str(action.get("node")), metadata)
    if not isinstance(meta, dict) or not meta.get("tag"):
        return False
    attrs = meta.get("attributes") or {}
    tag = str(meta["tag"]).lower()
    input_type = str(meta.get("input_type", meta.get("type", ""))).lower()
    label = str(action.get("label", "")).strip()
    name = re.sub(r"^Open ", "", label)
    if kind not in {"fill", "click", "select"} or input_type in {"password", "file", "hidden"}:
        return False
    if re.search(r"\b(book|booking|reserve|reservation|payment|sign in|log in|login|edit|publish|select flight)\b", label, re.I):
        return False
    for key in ("href", "form_action"):
        target = meta.get(key)
        if target:
            try:
                validate_url(task, urljoin(url, target))
            except ValueError:
                return False
    if str(meta.get("form_method") or "get").lower() != "get":
        return False
    if task.name == "hotel":
        identity = attrs.get("id", "")
        if tag == "input" and identity == "destination" and input_type == "search":
            return kind in {"fill", "click"}
        if tag == "select" and identity == "category":
            return kind == "select" and action.get("value") == "Design"
        if tag == "input" and identity == "free" and input_type == "checkbox":
            return kind == "click"
        return tag == "button" and kind == "click" and (
            (label == "Find stays" and meta.get("form_id") == "search" and input_type == "submit")
            or (label == "View Casa Flora" and attrs.get("data-place") == "Casa Flora")
        )
    if task.name == "wikipedia":
        if tag == "a" and kind == "click" and meta.get("href"):
            return unquote(urlsplit(urljoin(url, meta["href"])).path).startswith("/wiki/")
        search = attrs.get("name") == "search" or attrs.get("id") in {"searchInput", "searchInput2"}
        if tag == "input" and search and input_type in {"search", "text"}:
            return kind in {"fill", "click"}
        return tag in {"button", "input"} and kind == "click" and label in {"Search", "Go"} and bool(meta.get("form_action"))
    if task.name != "flights":
        return False
    role = action.get("role", attrs.get("role", ""))
    if kind == "fill":
        if name in {'Where else?', 'Where from?', 'Where to?'}:
            return (tag == 'input' and role == 'combobox'
                    and 'Select multiple airports' in meta.get('context', ''))
        return False
    if kind != "click":
        return False
    if name in {"Next", "Previous", "Done."}:
        return (tag == "button" and meta.get("label", "").strip() == name
                and "Underlined prices indicate cheapest compared with other prices shown" in meta.get("context", ""))
    if name in {"Where from?", "Where to?", "Departure", "One way", "Economy", "Search", "Done", "Next month", "Previous month"}:
        return True
    if re.fullmatch(r"(Change ticket type\. (Round trip|One way|Multi-city)|Change (?:cabin|seating) class\. Economy|\d+ passengers?(?:, change number of passengers\.)?|Adults|Increase adults|Decrease adults)", name, re.I):
        return True
    if re.fullmatch(r"Done\. Search for one-way flights, departing on .+", name):
        return True
    if role in {"option", "menuitem", "menuitemradio"}:
        return name in {'Zürich, Switzerland', 'London, United Kingdom', 'London (LON)', 'One way', 'Economy'}
    if role in {"button", "gridcell"}:
        day = date.fromisoformat(task.flight_date)
        expected = day.strftime('%A, %B ') + str(day.day) + day.strftime(', %Y')
        return name == expected
    return False


def filter_page(task: WebTask, page: dict, metadata: dict | None = None) -> dict:
    metadata = page.get("metadata", {}) if metadata is None else metadata
    calendar_open = task.name == 'flights' and any(
        a.get('label', '').strip() in {'Next', 'Previous', 'Done.'}
        and 'Underlined prices indicate cheapest compared with other prices shown' in
        metadata.get(str(a.get('node')), {}).get('context', '') for a in page.get('actions', []))
    return {**page, "completion": verify_task(task, page),
            "observed_controls": [{k: a[k] for k in ('kind', 'label', 'value', 'current_value', 'checked') if k in a}
                                  for a in page.get('actions', [])],
            "actions": [a for a in page.get("actions", [])
                        if not (calendar_open and a.get('kind') == 'scroll')
                        and allowed_action(task, a, metadata, page.get("url", ""))]}


def verify_task(task: WebTask, page: dict) -> dict:
    """Inspect a fresh browser observation; never consume the model's DONE claim."""
    url = page.get("url", "")
    try:
        validate_url(task, url)
        scoped = True
    except (ValueError, TypeError):
        scoped = False
    parsed = urlsplit(url)
    text = page.get("text", "")
    checks = {"scoped_url": scoped}
    if task.name == "wikipedia":
        checks["article_url"] = unquote(parsed.path) == WIKI_ARTICLE and not parsed.query
        title = str(page.get("title", "")).replace("’", "'")
        checks["article_content"] = (title.startswith("Gödel's incompleteness theorems")
            and len(text) >= 200 and "Gödel" in text and "incompleteness" in text.lower())
    elif task.name == "hotel":
        checks.update({
            "property_hash": parsed.fragment == "casa-flora",
            "property": "Casa Flora" in text,
            "filters": "Your filters: Design · Free cancellation enabled · Destination Lisbon" in text,
            "free_cancellation": "Free cancellation included" in text,
        })
    elif task.name == "flights":
        day = date.fromisoformat(task.flight_date)
        actions = page.get("actions", [])
        values = {a.get("label", "").strip(): a.get("value") for a in actions}
        flights = [a.get("label", "") for a in actions if "Select flight" in a.get("label", "")]
        encoded = parse_qs(parsed.query).get("tfs", [""])[0]
        try:
            encoded_date = task.flight_date.encode() in base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, binascii.Error):
            encoded_date = False
        checks.update({
            "search_page": parsed.path == "/travel/flights/search",
            "one_way": values.get("Change ticket type. One way") == "One way",
            "origin": values.get("Where from?") == "Zürich",
            "destination": values.get("Where to?") == "London",
            "date": values.get("Departure") == f"{day.strftime('%a')}, {day.strftime('%b')} {day.day}",
            "year": encoded_date or f"departing {task.flight_date}" in text,
            "results": bool(flights) and all(f"{day.strftime('%A')}, {day.strftime('%B')} {day.day}" in f for f in flights),
            "one_adult": any(re.fullmatch(r"1 (?:passenger|adult)(?: passenger)?(?:, change number of passengers\.)?", a.get("label", ""), re.I) for a in actions),
            "economy": any(a.get("label") in {"Economy", "Change cabin class. Economy", "Change seating class. Economy"} and a.get("value", "Economy") == "Economy" for a in actions),
        })
        return {"passed": all(checks.values()), "checks": checks, "visible_flights": flights}
    else:
        checks["known_task"] = False
    return {"passed": all(checks.values()), "checks": checks}
