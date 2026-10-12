import html as _html
import json
from pathlib import Path

import pandas as pd

from bing_maps_scraper import pipeline
from bing_maps_scraper.models import Page
from bing_maps_scraper.parsers import parse, parse_hours_text
from bing_maps_scraper.urls import classify, fetch_url

URL = "https://www.bing.com/maps?ss=ypid.920A17DCA391449A"

# ---- fixtures reproducing the real structures observed on live Bing Maps pages (Oct 2026) ----


def _entity_div(ypid, title, address, lat, lon, phone="", website=""):
    obj = {"geometryType": 1, "geometry": {"x": lon, "y": lat},
           "entity": {"title": title, "id": f"ypid:YN{ypid}", "address": address, "phone": phone,
                      "website": website, "primaryCategoryName": "Business to business", "openStatus": "Closed"},
           "routablePoint": {"latitude": lat, "longitude": lon}}
    return f'<div data-entity="{_html.escape(json.dumps(obj))}"></div>'


HOURS = (
    '<table><tr><td><span class="e_bold">Tuesday</span></td><td><div class="hrRangeGrp"><div class="hrRange">9 AM - 7 PM</div></div></td></tr>'
    '<tr><td>Wednesday</td><td><div class="hrRangeGrp"><div class="hrRange">9 AM - 1 PM</div><div class="hrRange">2 PM - 7 PM</div></div></td></tr>'
    '<tr><td>Sunday</td><td><div class="hrRangeGrp"><div class="hrRange">Closed</div></div></td></tr></table>')
BHOPAL = _entity_div("920A17DCA391449A", "Nibav Home Lifts - Bhopal", "F-20, Bagh Mugaliya Road, Huzur, MP",
                     23.188852310180664, 77.46564483642578, "078248 12121", "https://www.nibavlifts.com/home-lifts-bhopal/")
DOM = '<h2 class=" eh_title b_entityTitle"><span><a href="https://www.bing.com/alink/link?url=x">Nibav Home Lifts - Bhopal</a></span></h2>'

MD = """## [Nibav Home Lifts - Bhopal](https://www.bing.com/alink/link?url=https%3a%2f%2fwww.nibavlifts.com%2fhome-lifts-bhopal%2f&source=serp-local&ypid=YN920A17DCA391449A)

3.9/5 · Business to business in Huzur, India

F-20, Bagh Mugaliya Road, Huzur, MP

| Tuesday | 09:00 - 19:00 |
| --- | --- |
| Wednesday | 09:00 - 19:00 |
| Sunday | Closed |

[078248 12121](tel:07824812121)

[Directions](https://www.bing.com/maps/directions?rtp=~pos.23.188852310180664_77.46564483642578_F-20%2c+Bagh+Mugaliya+Road%2c+Huzur%2c+MP_Nibav+Home+Lifts+-+Bhopal_)

## About
"""


# ---- URLs ----
def test_classify_urls():
    i = classify("https://www.bing.com/maps?cp=12.9753~77.6068&lvl=16&ypid=YN8001x123456")
    assert i["kind"] == "maps_place" and i["ypid"] == "YN8001x123456"
    assert (i["lat"], i["lon"], i["lat_lon_source"]) == (12.9753, 77.6068, "url_cp")
    i = classify("https://www.bing.com/maps?q=acme&sp=point.12.9_77.6_Acme")
    assert i["lat_lon_source"] == "url_pin" and i["lon"] == 77.6
    assert classify("https://www.bing.com/search?q=acme+jewellers")["kind"] == "search_local"
    assert classify("https://example.com/x")["kind"] == "unknown"


def test_real_world_ss_ypid_format():
    i = classify("https://www.bing.com/maps?ss=ypid.B9BE9228E9C4E0D5")
    assert i["kind"] == "maps_place" and i["ypid"] == "B9BE9228E9C4E0D5"


def test_fetch_url_forces_english():
    assert fetch_url(URL) == "https://www.bing.com/maps/?ss=ypid.920A17DCA391449A&setlang=en&mkt=en-IN"
    assert fetch_url("https://example.com/x") == "https://example.com/x"


# ---- hours ----
def test_hours_parsing():
    h = parse_hours_text("Mon-Fri 10:00 AM - 8:00 PM\nSat 10 AM - 6 PM\nSunday Closed")
    assert h["Wed"] == "10:00-20:00" and h["Sat"] == "10:00-18:00" and h["Sun"] == "Closed"
    assert parse_hours_text("Monday: 09:30-17:45")["Mon"] == "09:30-17:45"
    assert parse_hours_text("Tuesday | 09:00 - 19:00 |")["Tue"] == "09:00-19:00"
    assert parse_hours_text("Mon 9 AM - 1 PM, 2 PM - 6 PM")["Mon"] == "09:00-13:00,14:00-18:00"


# ---- parsing ----
def test_entity_layer_exact_pin_and_hours():
    L = parse(Page(URL, html=f"<html>{BHOPAL}{DOM}{HOURS}</html>", fetcher="t"))
    assert L.status == "ok" and L.name == "Nibav Home Lifts - Bhopal"
    assert L.address == "F-20, Bagh Mugaliya Road, Huzur, MP" and L.website_domain == "nibavlifts.com"
    assert (L.latitude, L.longitude) == (23.188852310180664, 77.46564483642578)
    assert L.field_sources["latitude"] == "entity" and L.field_sources["hours"] == "dom"
    h = json.loads(L.hours_json)
    assert h["Tue"] == "09:00-19:00" and h["Wed"] == "09:00-13:00,14:00-19:00" and h["Sun"] == "Closed"
    assert L.pin_code == ""  # Bing publishes no PIN here: must stay blank, never invented


def test_entity_guard_never_returns_another_business():
    other = _entity_div("AAAAAAAAAAAAAAAA", "Some Other Shop", "1 Other St", 1.0, 2.0, "999")
    L = parse(Page(URL, html=f"<html>{other}</html>", fetcher="t"))  # only a neighbour present
    assert L.name != "Some Other Shop" and L.status == "partial" and "none matches" in L.error
    L = parse(Page(URL, html=f"<html>{other}{BHOPAL}</html>", fetcher="t"))  # target among neighbours
    assert L.name == "Nibav Home Lifts - Bhopal" and L.status == "ok"


def test_firecrawl_markdown_path():
    L = parse(Page(URL, markdown=MD, html="", fetcher="firecrawl"))
    assert L.status == "ok" and L.name == "Nibav Home Lifts - Bhopal"
    assert L.address == "F-20, Bagh Mugaliya Road, Huzur, MP" and L.field_sources["address"] == "directions"
    assert (L.latitude, L.longitude) == (23.188852310180664, 77.46564483642578)
    assert L.phone_digits == "07824812121" and L.website == "https://www.nibavlifts.com/home-lifts-bhopal/"
    assert json.loads(L.hours_json) == {"Tue": "09:00-19:00", "Wed": "09:00-19:00", "Sun": "Closed"}
    assert L.category == "Business to business"


def test_pin_extracted_only_from_address_text():
    html = _entity_div("920A17DCA391449A", "X", "12 MG Road, Bengaluru, KA 560001", 12.9, 77.6)
    L = parse(Page(URL, html=html, fetcher="t"))
    assert L.pin_code == "560001" and L.field_sources["pin_code"] == "address_regex"


def test_jsonld_layer():
    ld = ('<script type="application/ld+json">{"@type":"LocalBusiness","name":"Acme","address":{"streetAddress":"1 MG Road",'
          '"postalCode":"560001"},"telephone":"+91 80 1","geo":{"latitude":12.9,"longitude":77.6}}</script>')
    L = parse(Page("https://www.bing.com/search?q=acme", html=f"<html>{ld}</html>", fetcher="t"))
    assert L.pin_code == "560001" and L.field_sources["latitude"] == "jsonld" and L.status == "ok"


def test_url_latlon_is_last_resort_and_labelled():
    L = parse(Page("https://www.bing.com/maps?cp=1.5~2.5&ypid=YN1x1", html="<html><h2 class='b_entityTitle'>Z</h2></html>"))
    assert (L.latitude, L.longitude) == (1.5, 2.5) and L.field_sources["latitude"] == "url_cp"


def test_blocked_and_failed():
    assert parse(Page("https://www.bing.com/maps", html="<html>Please solve the CAPTCHA</html>")).status == "blocked"
    assert parse(Page("https://www.bing.com/maps", html="<html><body>hi</body></html>")).status == "failed"
    assert parse(Page("https://www.bing.com/maps", error="HTTP 500")).status == "failed"


def test_unresolvable_ypid_reports_clear_reason():
    L = parse(Page("https://www.bing.com/maps?ss=ypid.YN873x123", html="<html><body>map</body></html>",
                   xhr={"overlaybfpr": "<link rel='preconnect'/>"}, fetcher="playwright"))
    assert L.status == "failed" and "no listing card" in L.error


# ---- pipeline ----
def test_offline_reparse_pipeline_and_no_url_rows(tmp_path: Path):
    inp = tmp_path / "in.csv"
    pd.DataFrame({"businessId": ["1", "2"], "bingUrl": [URL, None]}).to_csv(inp, index=False)
    df = pipeline.read_inputs(inp)
    cache = tmp_path / "out" / "raw_cache"
    pipeline._save(cache, Page(URL, html=f"<html>{BHOPAL}{DOM}{HOURS}</html>", fetcher="t"))
    out = pipeline.run(df, None, cache)
    assert list(out["status"]) == ["ok", "no_url"] and out.loc[0, "in_businessId"] == "1"
    paths = pipeline.write(out, tmp_path / "out")
    assert all(p.exists() for p in paths.values())
