"""Layered extraction of listing fields from a fetched Bing page.

Layers, tried in order per field; the first to yield a value wins and its name is
recorded in Listing.field_sources:
  jsonld   - schema.org LocalBusiness/Place blocks
  embedded - JSON blobs inside <script> tags (keys like "latitude", "phone")
  dom      - Bing local-card / entity-panel markup (selectors are best-effort; refine
             against real cached pages with the `reparse` command)
  text     - regexes over visible text / Firecrawl markdown
  url      - lat/lon hints from the input URL (lat/lon only)
  llm      - Firecrawl structured extraction (only if enabled)
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .models import Listing, Page
from .urls import classify

PIN_PATTERNS = {"IN": r"(?<!\d)[1-9]\d{5}(?!\d)", "US": r"(?<!\d)\d{5}(?:-\d{4})?(?!\d)",
                "GB": r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_RE = {d: re.compile(rf"\b{full}\w*", re.I) for d, full in
           zip(DAYS, ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}
_BING_HOSTS = ("bing.com", "microsoft.com", "bing.net", "msn.com", "go.microsoft.com", "live.com")
_BLOCK_MARKERS = ("captcha", "unusual traffic", "verify you are a human", "g-recaptcha", "/fd/ls/ls.aspx")


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip(" ,‎‏")


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


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
    found = [(m.start(), d) for d, rx in _DAY_RE.items() for m in [rx.search(text)] if m]
    found.sort()
    codes = [d for _, d in found]
    if len(codes) == 2 and re.search(r"[-–—]|\bto\b|\bthrough\b", text):
        a, b = DAYS.index(codes[0]), DAYS.index(codes[1])
        return [DAYS[(a + i) % 7] for i in range((b - a) % 7 + 1)]
    return codes


def parse_hours_text(text: str) -> dict[str, str]:
    """Lines like 'Monday 9:00 AM - 6:00 PM', 'Mon-Fri: 10:00-19:00', 'Sunday Closed'."""
    out: dict[str, str] = {}
    for line in re.split(r"[\n;|]", text):
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
            times = [t.strip() for t in re.findall(r"\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?", rest, re.I) if t.strip()]
            if len(times) < 2:
                continue
            val = f"{to_24h(times[0])}-{to_24h(times[1])}"
        for d in days:
            out.setdefault(d, val)
    return out


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
        r: dict = {}
        r["name"] = _clean(n.get("name"))
        r["address"], pin = _addr_to_str(n.get("address"))
        r["pin_code"] = pin
        r["phone"] = _clean(n.get("telephone"))
        r["website"] = n.get("url") if n.get("url") and not _is_bing(n.get("url")) else ""
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


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_bing(u: str) -> bool:
    host = urlparse(u if "//" in u else "//" + u).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in _BING_HOSTS)


_LAT = re.compile(r'"(?:latitude|lat)"\s*:\s*"?(-?\d{1,2}\.\d{3,})"?', re.I)
_LON = re.compile(r'"(?:longitude|lon|lng|long)"\s*:\s*"?(-?\d{1,3}\.\d{3,})"?', re.I)


def _from_embedded(html: str, target_name: str = "") -> dict:
    """Bing inlines entity JSON in scripts/attributes. Take the lat/lon pair closest to the
    entity name when we know it, otherwise the first pair; this is the least certain layer."""
    r: dict = {}
    anchor = html.find(target_name) if target_name else -1
    best = None
    for m in _LAT.finditer(html):
        n = _LON.search(html, max(0, m.start() - 200), m.end() + 200)
        if n:
            lat, lon = float(m.group(1)), float(n.group(1))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                dist = abs(m.start() - anchor) if anchor >= 0 else m.start()
                if best is None or dist < best[0]:
                    best = (dist, lat, lon)
    if best:
        r["latitude"], r["longitude"] = best[1], best[2]
    return r


def _from_dom(soup: BeautifulSoup) -> dict:
    r: dict = {}
    sel = lambda *s: next((el for q in s for el in [soup.select_one(q)] if el and _clean(el.get_text())), None)  # noqa: E731
    el = sel(".b_entityTitle", "h2.b_entityTitle", "[class*=overlay-header] h1", "[class*=entityTitle]",
             "[data-tag*=title]", "h1")
    if el:
        r["name"] = _clean(el.get_text())
    el = sel(".b_address", "[class*=b_address]", "[class*=address]", "[data-tag*=address]")
    if el:
        r["address"] = _clean(el.get_text())
    tel = soup.select_one("a[href^='tel:']")
    if tel:
        r["phone"] = _clean(tel.get("href", "")[4:] or tel.get_text())
    for a in soup.find_all("a", href=True):
        label = (a.get_text() + " " + (a.get("aria-label") or "") + " " + " ".join(a.get("class") or [])).lower()
        if "website" in label and a["href"].startswith("http") and not _is_bing(a["href"]):
            r["website"] = a["href"]
            break
    el = sel("[class*=opHours]", "[class*=openHours]", "[class*=hours]", "[data-tag*=hours]")
    if el:
        r["hours_text"] = el.get_text("\n")
    for tag in soup.find_all(attrs={"data-lat": True}):
        la, lo = _f(tag.get("data-lat")), _f(tag.get("data-lon") or tag.get("data-lng"))
        if la is not None and lo is not None:
            r["latitude"], r["longitude"] = la, lo
            break
    return r


_PHONE_RX = re.compile(r"(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,5}\)?[\s-]?)?\d{3,5}[\s-]?\d{3,5}")


def _from_text(text: str, country: str) -> dict:
    r: dict = {}
    m = re.search(r"(?:Phone|Call|Tel)[^\d+]{0,15}(\+?[\d][\d\s()-]{7,18}\d)", text, re.I)
    if m:
        r["phone"] = _clean(m.group(1))
    m = re.search(r"(?:Address)\W{0,5}([^\n]{10,200})", text, re.I)
    if m:
        r["address"] = _clean(m.group(1))
    m = re.search(r"\[?Website\]?\((https?://[^)\s]+)\)", text, re.I)
    if m and not _is_bing(m.group(1)):
        r["website"] = m.group(1)
    block = re.search(r"((?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*[^\n]{0,60}\n?){2,})", text)
    if block:
        r["hours_text"] = block.group(1)
    return r


def _from_llm(j: dict | None) -> dict:
    if not j:
        return {}
    return {k: v for k, v in {
        "name": _clean(j.get("name")), "address": _clean(j.get("address")), "pin_code": _clean(j.get("postal_code")),
        "phone": _clean(j.get("phone")), "website": j.get("website") if j.get("website") and not _is_bing(j["website"]) else "",
        "hours_text": j.get("opening_hours") or "", "latitude": _f(j.get("latitude")), "longitude": _f(j.get("longitude")),
    }.items() if v not in ("", None)}


def parse(page: Page, country: str = "IN") -> Listing:
    info = classify(page.url)
    L = Listing(input_url=page.url, url_kind=info["kind"], ypid=info["ypid"], fetcher=page.fetcher)
    if page.error or not (page.html or page.markdown):
        L.status, L.error = "failed", page.error or "empty response"
        return L
    blob = (page.html + page.markdown).lower()
    soup = BeautifulSoup(page.html or "", "lxml")
    visible = page.markdown or soup.get_text("\n")

    layers: list[tuple[str, dict]] = [("jsonld", _from_jsonld(soup)), ("dom", _from_dom(soup)),
                                      ("text", _from_text(visible, country))]
    name_hint = next((d["name"] for _, d in layers if d.get("name")), "")
    layers.insert(1, ("embedded", _from_embedded(page.html, name_hint)))
    layers.append(("url", {"latitude": info["lat"], "longitude": info["lon"]} if info["lat"] is not None else {}))
    layers.append(("llm", _from_llm(page.llm_json)))

    def pick(key: str):
        for src, d in layers:
            if d.get(key) not in (None, "", {}):
                L.field_sources[key] = src if not (key in ("latitude", "longitude") and src == "url")\
                    else info["lat_lon_source"]
                return d[key]
        return None

    L.name, L.address = pick("name") or "", pick("address") or ""
    L.phone_raw = pick("phone") or ""
    L.phone_digits = digits(L.phone_raw)
    L.website = pick("website") or ""
    L.website_domain = urlparse(L.website).netloc.lower().removeprefix("www.") if L.website else ""
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
        raw = next(((s, d["hours_text"]) for s, d in layers if d.get("hours_text")), None)
        if raw:
            hours = parse_hours_text(raw[1])
            L.hours_raw = _clean(raw[1])
            L.field_sources["hours"] = raw[0]
    if hours:
        L.hours_json = json.dumps({d: hours[d] for d in DAYS if d in hours})
        L.hours_raw = L.hours_raw or "; ".join(f"{d} {hours[d]}" for d in DAYS if d in hours)

    if any(m in blob for m in _BLOCK_MARKERS) and not (L.name and L.address):
        L.status, L.error = "blocked", "captcha / consent / bot-check page detected"
    elif L.name and L.address:
        L.status = "ok"
    elif L.name or L.address or L.phone_raw:
        L.status, L.error = "partial", "missing " + ", ".join(k for k, v in (("name", L.name), ("address", L.address)) if not v)
    else:
        L.status, L.error = "failed", "no listing fields found (JS not rendered, wrong URL type, or selectors need calibration)"
    return L
