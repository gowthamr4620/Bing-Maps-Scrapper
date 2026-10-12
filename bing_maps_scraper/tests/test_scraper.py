import json
from pathlib import Path

import pandas as pd

from bing_maps_scraper import pipeline
from bing_maps_scraper.models import Page
from bing_maps_scraper.parsers import parse, parse_hours_text
from bing_maps_scraper.urls import classify

JSONLD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"LocalBusiness","name":"Acme Jewellers",
 "address":{"streetAddress":"12 MG Road","addressLocality":"Bengaluru","addressRegion":"KA","postalCode":"560001"},
 "telephone":"+91 80 4123 4567","url":"https://www.acmejewel.in/",
 "geo":{"latitude":12.9753,"longitude":77.6068},
 "openingHoursSpecification":[{"dayOfWeek":["Monday","Tuesday"],"opens":"10:00","closes":"20:00"}]}
</script></head><body></body></html>"""

CARD = """<html><body><div class="b_entityTP"><h2 class="b_entityTitle">Acme Jewellers</h2>
<div class="b_address">12 MG Road, Bengaluru, Karnataka 560001</div>
<a href="tel:+918041234567">Call</a>
<a href="https://www.acmejewel.in/" class="b_clickarea">Website</a>
<a href="https://www.bing.com/ck/a?x">Website</a>
<div class="opHours">Mon-Fri 10:00 AM - 8:00 PM
Sat 10 AM - 6 PM
Sun Closed</div>
<script>{"latitude":"12.97531","longitude":"77.60681"}</script></div></body></html>"""


def test_classify_urls():
    u = "https://www.bing.com/maps?cp=12.9753~77.6068&lvl=16&ypid=YN8001x123456"
    i = classify(u)
    assert i["kind"] == "maps_place" and i["ypid"] == "YN8001x123456"
    assert (i["lat"], i["lon"], i["lat_lon_source"]) == (12.9753, 77.6068, "url_cp")
    i = classify("https://www.bing.com/maps?q=acme&sp=point.12.9_77.6_Acme")
    assert i["lat_lon_source"] == "url_pin" and i["lon"] == 77.6
    assert classify("https://www.bing.com/search?q=acme+jewellers+bangalore")["kind"] == "search_local"
    assert classify("https://example.com/x")["kind"] == "unknown"


def test_hours_parsing():
    h = parse_hours_text("Mon-Fri 10:00 AM - 8:00 PM\nSat 10 AM - 6 PM\nSunday Closed")
    assert h["Wed"] == "10:00-20:00" and h["Sat"] == "10:00-18:00" and h["Sun"] == "Closed"
    assert parse_hours_text("Monday: 09:30-17:45")["Mon"] == "09:30-17:45"


def test_jsonld_layer():
    L = parse(Page("https://www.bing.com/maps?ypid=YN1x2", html=JSONLD, fetcher="t"))
    assert (L.name, L.pin_code, L.phone_digits, L.website_domain) == ("Acme Jewellers", "560001", "918041234567", "acmejewel.in")
    assert (L.latitude, L.longitude) == (12.9753, 77.6068)
    assert L.field_sources["latitude"] == "jsonld" and json.loads(L.hours_json)["Tue"] == "10:00-20:00"
    assert L.status == "partial" or L.status == "ok"


def test_dom_layer_and_bing_website_excluded():
    L = parse(Page("https://www.bing.com/search?q=acme", html=CARD, fetcher="t"))
    assert L.status == "ok" and L.name == "Acme Jewellers"
    assert L.pin_code == "560001" and L.field_sources["pin_code"] == "address_regex"
    assert L.website == "https://www.acmejewel.in/"
    assert L.phone_digits == "918041234567"
    assert (L.latitude, L.longitude) == (12.97531, 77.60681) and L.field_sources["latitude"] == "embedded"
    assert json.loads(L.hours_json)["Sun"] == "Closed"


def test_url_latlon_is_last_resort_and_labelled():
    L = parse(Page("https://www.bing.com/maps?cp=1.5~2.5&ypid=YN1x1", html=CARD.replace('<script>{"latitude":"12.97531","longitude":"77.60681"}</script>', ""), fetcher="t"))
    assert (L.latitude, L.longitude) == (1.5, 2.5) and L.field_sources["latitude"] == "url_cp"


def test_blocked_and_failed():
    assert parse(Page("https://www.bing.com/maps", html="<html>Please solve the CAPTCHA</html>")).status == "blocked"
    assert parse(Page("https://www.bing.com/maps", html="<html><body>hi</body></html>")).status == "failed"
    assert parse(Page("https://www.bing.com/maps", error="HTTP 500")).status == "failed"


def test_pipeline_offline_reparse(tmp_path: Path):
    inp = tmp_path / "in.csv"
    pd.DataFrame({"store_id": ["S1"], "Bing Maps Link": ["https://www.bing.com/search?q=acme"]}).to_csv(inp, index=False)
    df = pipeline.read_inputs(inp)
    cache = tmp_path / "out" / "raw_cache"
    pipeline._save(cache, Page(df.url[0], html=CARD, fetcher="t"))
    out = pipeline.run(df, None, cache)
    assert out.loc[0, "in_store_id"] == "S1" and out.loc[0, "status"] == "ok"
    paths = pipeline.write(out, tmp_path / "out")
    assert all(p.exists() for p in paths.values())


def test_real_world_ss_ypid_format_and_blank_rows(tmp_path):
    i = classify("https://www.bing.com/maps?ss=ypid.B9BE9228E9C4E0D5")
    assert i["kind"] == "maps_place" and i["ypid"] == "B9BE9228E9C4E0D5"
    inp = tmp_path / "in.csv"
    pd.DataFrame({"businessId": ["1", "2"], "bingUrl": ["https://www.bing.com/maps?ss=ypid.AB12", None]}).to_csv(inp, index=False)
    df = pipeline.read_inputs(inp)
    out = pipeline.run(df, None, tmp_path / "c")
    assert list(out["status"]) == ["failed", "no_url"] and list(out["in_businessId"]) == ["1", "2"]
