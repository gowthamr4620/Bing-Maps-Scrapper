"""Layered extraction of listing fields from a fetched Bing page.

Layers, in priority order per field; the first to yield a value wins and its name is
recorded in Listing.field_sources:
  entity     - Bing's own entity JSON ({"geometry":..., "entity":{title,address,phone,website}})
               embedded in the page / `overlaybfpr` XHR. Only accepted when its id matches the
               input ypid (the page also carries *other* businesses).
  jsonld     - schema.org blocks, if Bing ever emits them
  dom        - rendered panel markup (h2.b_entityTitle, tel: link, hrRange hours table)
  directions - the Directions link: rtp=~pos.LAT_LON_ADDRESS_NAME_  (exact pin + name + address)
  text       - Firecrawl markdown / visible text (## name, (tel:...), hours table, alink website)
  url        - lat/lon hint from the input URL (labelled url_pin / url_cp)
  llm        - Firecrawl structured extraction (only if enabled)

Bing does not show a postal code in the listing panel; pin_code is therefore only filled when
the address string itself contains one (source `address_regex`) and is otherwise left blank.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .models import Listing, Page
from .urls import classify

PIN_PATTERNS = {"IN": r"(?<!\d)[1-9]\d{5}(?!\d)", "US": r"(?<!\d)\d{5}(?:-\d{4})?(?!\d)",
                "GB": r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_RE = {d: re.compile(rf"\b{full}\w*", re.I) for d, full in
           zip(DAYS, ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}
_BING_HOSTS = ("bing.com", "microsoft.com", "bing.net", "msn.com", "live.com", "bingplaces.com")
_BLOCK_MARKERS = ("captcha", "unusual traffic", "verify you are a human", "g-recaptcha", "/fd/ls/ls.aspx")
_TIME_RX = r"\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?"


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip(" ,‎‏")


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_bing(u: str) -> bool:
    host = urlparse(u if "//" in u else "//" + u).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in _BING_HOSTS)


# ---------------------------------------------------------------- hours
def to_24h(t: str) -> str:
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", t, re.I)
    if not m:
        return t.strip()
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}"


def _day_codes(text: str) -> list[str]:
    """'Mon-Fri' / 'Monday to Friday' / 'Sat' -> list of day codes."""
    found = sorted((m.start(), d) for d, rx in _DAY_RE.items() for m in [rx.search(text)] if m)
    codes = [d for _, d in found]
    if len(codes) == 2 and re.search(r"[-–—]|\bto\b|\bthrough\b", text):
        a, b = DAYS.index(codes[0]), DAYS.index(codes[1])
        return [DAYS[(a + i) % 7] for i in range((b - a) % 7 + 1)]
    return codes


def parse_hours_text(text: str) -> dict[str, str]:
    """Lines like 'Monday 9:00 AM - 6:00 PM', 'Mon-Fri: 10:00-19:00', 'Sunday Closed',
    'Tuesday | 09:00 - 19:00' (markdown table) or split shifts '9 AM - 1 PM, 2 PM - 7 PM'."""
    out: dict[str, str] = {}
    for line in re.split(r"[\n;]", text.replace("|", " ")):
        cut = re.search(r"\d|closed|open 24", line, re.I)
        label, rest = (line[:cut.start()], line[cut.start():]) if cut else (line, "")
        days = _day_codes(label)
        if not days:
            continue
        if re.search(r"\bclosed\b", rest, re.I):
            val = "Closed"
        elif re.search(r"24\s*hours|open 24", rest, re.I):
            val = "00:00-24:00"
        else:
            times = [t.strip() for t in re.findall(_TIME_RX, rest, re.I) if t.strip()]
            if len(times) < 2:
                continue
            val = ",".join(f"{to_24h(a)}-{to_24h(b)}" for a, b in zip(times[0::2], times[1::2]))
        for d in days:
            out.setdefault(d, val)
    return out


def _hours_from_soup(soup: BeautifulSoup) -> str:
    """Bing's panel: <tr><td>Tuesday</td><td><div class=hrRangeGrp><div class=hrRange>9 AM - 7 PM</div>..."""
    lines = []
    for tr in soup.select("tr"):
        tds = tr.find_all("td")
        if len(tds) == 2 and _day_codes(tds[0].get_text()) and re.search(r"\d|closed", tds[1].get_text(), re.I):
            ranges = [r.get_text(" ") for r in tds[1].select(".hrRange")] or [tds[1].get_text(" ")]
            lines.append(f"{_clean(tds[0].get_text())} {', '.join(_clean(r) for r in ranges)}")
    return "\n".join(lines) if len(lines) >= 1 else ""


# ---------------------------------------------------------------- layers
def _ypid_norm(s: str) -> str:
    s = re.sub(r"^ypid[:.]", "", s or "", flags=re.I).upper()
    return s[2:] if s.startswith("YN") else s


def _entities(text: str) -> list[dict]:
    """All Bing entity records ({"geometry":{x,y}, "entity":{id,title,...}}) found in page/XHR text.
    They sit HTML-entity-escaped inside attributes, so unescape first."""
    t = htmllib.unescape(text)
    dec, out = json.JSONDecoder(), []
    for m in re.finditer(r'\{"geometryType"\s*:', t):
        try:
            obj, _ = dec.raw_decode(t[m.start():])
        except ValueError:
            continue
        if isinstance(obj.get("entity"), dict):
            out.append(obj)
    return out


def _from_entity(text: str, ypid: str) -> tuple[dict, str]:
    """Return (fields, warning). Never returns another business's record."""
    ents = _entities(text)
    if not ents:
        return {}, ""
    want = _ypid_norm(ypid)
    pick = None
    for o in ents:
        eid = _ypid_norm(str(o["entity"].get("id", "")))
        if want and (eid == want or want in eid or eid in want):
            pick = o
            break
    if pick is None:
        if len(ents) == 1 and not want:
            pick = ents[0]
        else:
            return {}, f"{len(ents)} entities on page, none matches ypid {ypid or '?'}"
    e, g = pick["entity"], pick.get("geometry") or {}
    r = {"name": _clean(e.get("title")), "address": _clean(e.get("address")), "phone": _clean(e.get("phone")),
         "website": e.get("website") if e.get("website") and not _is_bing(e["website"]) else "",
         "category": _clean(e.get("primaryCategoryName")), "open_status": _clean(e.get("openStatus")),
         "latitude": _f(g.get("y")), "longitude": _f(g.get("x"))}
    if r["latitude"] is None:
        rp = pick.get("routablePoint") or {}
        r["latitude"], r["longitude"] = _f(rp.get("latitude")), _f(rp.get("longitude"))
    return {k: v for k, v in r.items() if v not in ("", None)}, ""


def _iter_jsonld(soup: BeautifulSoup):
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            n = stack.pop()
            if isinstance(n, list):
                stack.extend(n)
            elif isinstance(n, dict):
                t = n.get("@type")
                types = t if isinstance(t, list) else [t]
                if any(isinstance(x, str) and re.search(r"Business|Place|Store|Restaurant|Organization|Hotel|Bank|Clinic", x) for x in types):
                    yield n
                stack.extend(v for v in n.values() if isinstance(v, (dict, list)))


def _addr_to_str(a) -> tuple[str, str]:
    if isinstance(a, str):
        return _clean(a), ""
    if isinstance(a, dict):
        parts = [a.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")]
        return _clean(", ".join(str(p) for p in parts if p and not isinstance(p, dict))), _clean(a.get("postalCode"))
    return "", ""


def _from_jsonld(soup: BeautifulSoup) -> dict:
    for n in _iter_jsonld(soup):
        r: dict = {"name": _clean(n.get("name")), "phone": _clean(n.get("telephone")),
                   "website": n.get("url") if n.get("url") and not _is_bing(n.get("url")) else ""}
        r["address"], r["pin_code"] = _addr_to_str(n.get("address"))
        geo = n.get("geo") or {}
        if isinstance(geo, dict) and geo.get("latitude") not in (None, ""):
            r["latitude"], r["longitude"] = _f(geo.get("latitude")), _f(geo.get("longitude"))
        spec = n.get("openingHoursSpecification")
        hrs: dict[str, str] = {}
        for s in spec if isinstance(spec, list) else [spec] if spec else []:
            if not isinstance(s, dict):
                continue
            days = s.get("dayOfWeek")
            for d in days if isinstance(days, list) else [days]:
                code = next((c for c in DAYS if isinstance(d, str) and d.lower().rstrip("/").split("/")[-1].startswith(c.lower())), None)
                if code:
                    hrs[code] = f"{s.get('opens', '')[:5]}-{s.get('closes', '')[:5]}"
        oh = n.get("openingHours")
        if not hrs and oh:
            hrs = parse_hours_text("\n".join(oh if isinstance(oh, list) else [oh]))
        r["hours"] = hrs
        if r["name"] or r["address"]:
            return {k: v for k, v in r.items() if v not in ("", None, {})}
    return {}


def _alink_website(text: str) -> str:
    """Bing wraps the business website as bing.com/alink/link?url=<site>&..."""
    for m in re.finditer(r"bing\.com/alink/link\?([^\"'\s)<]+)", htmllib.unescape(text)):
        v = parse_qs(m.group(1)).get("url")
        if v and v[0].startswith("http") and not _is_bing(v[0]):
            return v[0]
    return ""


def _from_directions(text: str) -> dict:
    """href .../maps/directions?rtp=~pos.<lat>_<lon>_<address>_<name>_"""
    m = re.search(r"directions\?[^\"'\s)<]*?rtp=~pos\.([^\"'\s)<&]+)", htmllib.unescape(text))
    if not m:
        return {}
    parts = unquote(m.group(1).replace("+", " ")).split("_")
    if len(parts) < 3 or _f(parts[0]) is None or _f(parts[1]) is None:
        return {}
    lat, lon = float(parts[0]), float(parts[1])
    tail = [p for p in parts[2:] if p]
    r = {"latitude": lat, "longitude": lon}
    if tail:
        r["address"] = _clean(tail[0])
    if len(tail) > 1:
        r["name"] = _clean(tail[-1])
    return r


def _from_dom(soup: BeautifulSoup) -> dict:
    r: dict = {}
    el = soup.select_one("h2.b_entityTitle, .b_entityTitle, [class*=eh_title]")
    if el and _clean(el.get_text()):
        r["name"] = _clean(el.get_text())
    tel = soup.select_one("a[href^='tel:']")
    if tel:
        r["phone"] = _clean(tel.get("href", "")[4:] or tel.get_text())
    h = _hours_from_soup(soup)
    if h:
        r["hours_text"] = h
    return r


def _from_text(text: str) -> dict:
    """Firecrawl markdown / visible text."""
    r: dict = {}
    names = [x for x in re.findall(r"^##\s+\[?([^\]\n(]+?)\]?(?:\(|\s{2,}|$)", text, re.M) if x.strip().lower() != "about"]
    if names:
        r["name"] = _clean(names[0])
    m = re.search(r"\(tel:([^)\s]+)\)", text)
    if m:
        r["phone"] = _clean(m.group(1))
    else:
        m = re.search(r"(?:Phone|Call|Tel)[^\d+]{0,15}(\+?\d[\d\s()-]{7,18}\d)", text, re.I)
        if m:
            r["phone"] = _clean(m.group(1))
    m = re.search(r"·\s*([^\n·]+?)\s+in\s+[^\n]+$", text, re.M)
    if m:
        r["category"] = _clean(m.group(1))
    site = _alink_website(text)
    if site:
        r["website"] = site
    block = re.findall(r"^\|?\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s*\|[^\n]*$", text, re.M)
    if len(block) >= 2:
        r["hours_text"] = "\n".join(block)
    return r


def _from_llm(j: dict | None) -> dict:
    if not j:
        return {}
    return {k: v for k, v in {
        "name": _clean(j.get("name")), "address": _clean(j.get("address")), "pin_code": _clean(j.get("postal_code")),
        "phone": _clean(j.get("phone")), "website": j.get("website") if j.get("website") and not _is_bing(j["website"]) else "",
        "hours_text": j.get("opening_hours") or "", "latitude": _f(j.get("latitude")), "longitude": _f(j.get("longitude")),
    }.items() if v not in ("", None)}


# ---------------------------------------------------------------- main
def parse(page: Page, country: str = "IN") -> Listing:
    info = classify(page.url)
    L = Listing(input_url=page.url, url_kind=info["kind"], ypid=info["ypid"], fetcher=page.fetcher)
    if page.error or not (page.html or page.markdown or page.xhr):
        L.status, L.error = "failed", page.error or "empty response"
        return L
    raw = "\n".join([page.html, page.markdown, *page.xhr.values()])
    soup = BeautifulSoup("\n".join([page.html, *page.xhr.values()]), "lxml")

    entity, warn = _from_entity(raw, info["ypid"])
    # With a matched entity, the Directions link of that page is trustworthy too; without one it may
    # belong to a neighbouring business, so only use it if the page had no competing entities.
    layers: list[tuple[str, dict]] = [("entity", entity), ("jsonld", _from_jsonld(soup)), ("dom", _from_dom(soup))]
    layers.append(("directions", _from_directions(raw) if not warn else {}))
    layers.append(("text", _from_text(page.markdown or soup.get_text("\n"))))
    site = _alink_website(raw)
    if site and not warn:
        layers.append(("alink", {"website": site}))
    layers.append(("url", {"latitude": info["lat"], "longitude": info["lon"]} if info["lat"] is not None else {}))
    layers.append(("llm", _from_llm(page.llm_json)))

    def pick(key: str):
        for src, d in layers:
            if d.get(key) not in (None, "", {}):
                L.field_sources[key] = info["lat_lon_source"] if (src == "url" and key in ("latitude", "longitude")) else src
                return d[key]
        return None

    L.name, L.address = pick("name") or "", pick("address") or ""
    L.phone_raw = pick("phone") or ""
    L.phone_digits = digits(L.phone_raw)
    L.website = pick("website") or ""
    L.website_domain = urlparse(L.website).netloc.lower().removeprefix("www.") if L.website else ""
    L.category, L.open_status = pick("category") or "", pick("open_status") or ""
    L.latitude, L.longitude = pick("latitude"), pick("longitude")
    L.pin_code = pick("pin_code") or ""
    if not L.pin_code and L.address:
        found = re.findall(PIN_PATTERNS.get(country, PIN_PATTERNS["IN"]), L.address)
        if found:
            L.pin_code, L.field_sources["pin_code"] = found[-1], "address_regex"

    hours = next((d["hours"] for _, d in layers if d.get("hours")), None)
    if hours:
        L.field_sources["hours"] = next(s for s, d in layers if d.get("hours"))
    else:
        raw_h = next(((s, d["hours_text"]) for s, d in layers if d.get("hours_text")), None)
        if raw_h:
            hours = parse_hours_text(raw_h[1])
            L.hours_raw = _clean(raw_h[1])
            L.field_sources["hours"] = raw_h[0]
    if hours:
        L.hours_json = json.dumps({d: hours[d] for d in DAYS if d in hours})
        L.hours_raw = "; ".join(f"{d} {hours[d]}" for d in DAYS if d in hours)

    blob = raw.lower()
    if warn:
        L.status, L.error = "partial", warn
    elif any(m in blob for m in _BLOCK_MARKERS) and not (L.name and L.address):
        L.status, L.error = "blocked", "captcha / consent / bot-check page detected"
    elif L.name and L.address:
        L.status = "ok"
    elif L.name or L.address or L.phone_raw:
        L.status, L.error = "partial", "missing " + ", ".join(k for k, v in (("name", L.name), ("address", L.address)) if not v)
    else:
        L.status, L.error = "failed", "no listing fields found (JS not rendered, wrong URL type, or selectors need calibration)"
    return L
