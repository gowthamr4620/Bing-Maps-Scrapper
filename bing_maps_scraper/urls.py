"""Classify and pick apart Bing / Bing Maps URLs."""
from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs, unquote

_NUM = r"-?\d{1,3}(?:\.\d+)?"


def _qs(url: str) -> dict[str, str]:
    q = parse_qs(urlparse(url).query, keep_blank_values=True)
    return {k.lower(): v[0] for k, v in q.items()}


def _latlon_from(value: str) -> tuple[float, float] | None:
    """Handles 'lat~lon' (cp=), 'lat,lon' and 'lat_lon' (point./pt. params)."""
    m = re.match(rf"^\s*({_NUM})\s*[~,_]\s*({_NUM})", unquote(value))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def classify(url: str) -> dict:
    """Return {kind, ypid, query, lat, lon, lat_lon_source}.

    kind: maps_place | maps_search | search_local | share_link | unknown
    lat/lon from the URL are a *hint only*: `cp=` is the map centre, not
    necessarily the business pin, so the source is reported alongside.
    """
    u = urlparse(url.strip())
    host, path, q = u.netloc.lower(), u.path.lower(), _qs(url)
    info = {"kind": "unknown", "ypid": "", "query": q.get("q", ""), "lat": None, "lon": None, "lat_lon_source": ""}

    if "bing.com" not in host and not host.endswith("bing.net") and "binged.it" not in host:
        return info

    # Observed formats: ?ss=ypid.<HEX>  |  ?ypid=YN123x456  |  ...&ss=ypid.YN873x...
    ypid = q.get("ypid") or ""
    if not ypid:
        m = re.search(r"ypid[.=]([A-Za-z0-9]+)", url, re.I) or re.search(r"(YN\d+x\d+)", url)
        ypid = m.group(1) if m else ""
    info["ypid"] = ypid

    if "binged.it" in host or "osid" in q or "/maps/" in path and "share" in path:
        info["kind"] = "share_link"
    elif path.startswith("/maps/place"):
        info["kind"] = "maps_place"
    elif path.startswith("/maps"):
        info["kind"] = "maps_place" if (ypid or "ss" in q) else "maps_search"
    elif path.startswith("/search") or path == "/":
        info["kind"] = "search_local"

    # Pin hints: pt.LAT_LON / point.LAT_LON_Name (also inside sp=), most specific first.
    blob = " ".join([k + "=" + v for k, v in q.items()])
    m = re.search(rf"(?:^|[ .=])(?:pt|point)[._](?:adr\.)?({_NUM})[_~,]({_NUM})", blob, re.I)
    if m and -90 <= float(m.group(1)) <= 90:
        info.update(lat=float(m.group(1)), lon=float(m.group(2)), lat_lon_source="url_pin")
    elif "cp" in q and (ll := _latlon_from(q["cp"])):
        info.update(lat=ll[0], lon=ll[1], lat_lon_source="url_cp")
    return info
