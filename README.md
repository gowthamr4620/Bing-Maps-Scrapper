# Bing-Maps-Scrapper (`bing_maps_scraper`)

Scrapes what Bing / Bing Maps *publicly displays* for a listing, so it can be compared
against what was pushed to Bing via API.

Fields: business name, address, PIN code, phone (raw + digits-only), website (+domain),
latitude, longitude, working hours (raw + normalised JSON), plus `ypid`, URL type,
status and **per-field provenance** (`field_sources`).

## Run

```bash
pip install -r requirements.txt
export FIRECRAWL_API_KEY=fc-...          # same Firecrawl account as the MCP connector

# input: CSV/XLSX with a `url` column (extra columns such as store_id are carried through
# as in_<col>), or a .txt with one URL per line
python -m bing_maps_scraper scrape input.csv -o output/ --country IN

python -m bing_maps_scraper reparse input.csv -o output/   # offline, from cached pages
```

Outputs in `output/`: `listings.xlsx` (listings, status_summary, field_coverage),
`listings.csv`, `listings.jsonl`, and `raw_cache/` (every fetched page, so nothing
needs re-scraping to fix a parser).

Fetchers: `firecrawl` (default when the key is set; REST API, JS-rendered), `playwright`
(`pip install playwright`), `http` (plain GET; poor for JS-heavy Bing Maps).
`--llm-extract` asks Firecrawl for LLM extraction as a last-resort fallback (costs extra credits).

## Status values
`ok` name+address found · `partial` some fields · `blocked` captcha/bot-check ·
`failed` fetch error or nothing parseable.

## What was verified against live Bing Maps (Oct 2026, 106 Nibav listings)
- 103 `ok`, 3 `failed`, 2 `no_url` (blank input cells). Every `ok` row's name/address/lat/lon came from Bing's
  own entity record, never from the URL fallback.
- Cross-checked two listings independently through the Firecrawl connector: identical name, address, phone,
  hours and lat/lon (to the last digit) to the headless-browser capture.
- Parsing layers (see `parsers.py`): `entity` (Bing's JSON, id must match the input ypid) -> `jsonld` -> `dom` ->
  `directions` link -> `text` (Firecrawl markdown) -> `url` -> `llm`. `field_sources` records the winner per field.

## Things to know when interpreting the data
- **PIN code is not a Bing field.** It is only present when Bing's address string contains one (84 of 103 here);
  otherwise it is blank. It is never inferred.
- **`open_status` is a snapshot** of "open now / closed" at scrape time. Don't compare it with pushed data.
  Use `hours_json` for that.
- **Unresolvable IDs.** `ypid.YN873x...` / `YN4070x...` style IDs rendered no listing card in either the browser
  or Firecrawl: status `failed`, reason says so. Treat as "not live on Bing", not as a scraper bug.
- **Latitude/longitude** come from `geometry` in the entity record (the pin). `url_cp` in `field_sources` would mean
  only the map centre was available; none occurred in this run.
- Bing localises by request region (IN requests came back in Hindi); URLs are rewritten with
  `setlang=en&mkt=en-IN`. Phones come as Bing shows them (`078248 12121` or `+91...`): normalise at comparison time.
- Fetchers: `playwright` (default without a key; needs Chromium, set `CHROMIUM_PATH` if not auto-found) and `firecrawl`
  (REST, needs `FIRECRAWL_API_KEY`; parsed from its markdown). Be gentle: `--workers 3` took ~13 min for 106 pages.

Tests: `python -m pytest bing_maps_scraper/tests`

## Input shape (from the Nibav sample)
One row per listing, e.g. `businessId, businessName, address, state, city, googleMapUrl, bingUrl`
with `bingUrl` like `https://www.bing.com/maps?ss=ypid.<HEX>`. The URL column is auto-detected
(`url`, `bingUrl`, ...); all other columns are carried through as `in_<column>` so the
reference values sit next to the scraped ones. Blank URLs are kept with status `no_url`.
Don't commit client input files (`*.xlsx` is git-ignored).
