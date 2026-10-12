"""Read input URLs -> fetch (cached) -> parse -> write CSV / XLSX / JSONL."""
from __future__ import annotations

import hashlib
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from .fetchers import Fetcher
from .models import Listing, Page
from .parsers import parse

URL_COLS = ("url", "bingurl", "link", "bing_url", "bing_link", "bing_maps_url", "bing maps link", "bing link")


def read_inputs(path: Path) -> pd.DataFrame:
    """CSV / XLSX with a URL column (any extra columns, e.g. store id, are passed through),
    or a .txt file with one URL per line."""
    if path.suffix.lower() == ".txt":
        urls = [u.strip() for u in path.read_text().splitlines() if u.strip() and not u.startswith("#")]
        return pd.DataFrame({"url": urls})
    df = pd.read_excel(path, dtype=str) if path.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(path, dtype=str)
    col = next((c for c in df.columns if c.strip().lower() in URL_COLS), None)
    if col is None:
        col = next((c for c in df.columns if df[c].astype(str).str.contains("bing.com|binged.it", na=False).any()), None)
    if col is None:
        raise SystemExit(f"No URL column found in {path}; name it 'url'. Columns: {list(df.columns)}")
    df = df.rename(columns={col: "url"})
    df["url"] = df["url"].fillna("").astype(str).str.strip()
    return df.reset_index(drop=True)  # blank/non-http rows are kept and reported as status "no_url"


def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def _cache_path(cache: Path, url: str) -> Path:
    return cache / f"{_key(url)}.json"


def _save(cache: Path, p: Page) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    _cache_path(cache, p.url).write_text(json.dumps(p.__dict__, ensure_ascii=False))


def _load(cache: Path, url: str) -> Page | None:
    f = _cache_path(cache, url)
    return Page(**json.loads(f.read_text())) if f.exists() else None


def fetch_one(fetcher: Fetcher, url: str, cache: Path, refetch: bool, delay: float) -> Page:
    if not refetch and (p := _load(cache, url)) and not p.error and (p.html or p.markdown):
        return p
    time.sleep(random.uniform(0, delay))
    p = fetcher.fetch(url)
    if not p.error and (p.html or p.markdown):
        _save(cache, p)
    return p


def run(df: pd.DataFrame, fetcher: Fetcher | None, cache: Path, country: str = "IN",
        workers: int = 4, refetch: bool = False, delay: float = 1.0) -> pd.DataFrame:
    """fetcher=None -> offline reparse of cached pages only."""
    def work(url: str) -> Listing:
        if not url.startswith("http"):
            return Listing(input_url=url, status="no_url", error="blank or non-http URL in input")
        if fetcher is None:
            page = _load(cache, url) or Page(url, error="not in cache (run scrape first)")
        else:
            page = fetch_one(fetcher, url, cache, refetch, delay)
        return parse(page, country)

    urls = df["url"].tolist()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        listings = list(ex.map(work, urls))
    out = pd.DataFrame([l.to_row() for l in listings])
    extra = df.drop(columns=["url"]).reset_index(drop=True)
    out = pd.concat([extra.add_prefix("in_") if len(extra.columns) else extra, out], axis=1)
    return out


def write(out: pd.DataFrame, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"csv": out_dir / "listings.csv", "xlsx": out_dir / "listings.xlsx", "jsonl": out_dir / "listings.jsonl"}
    out.to_csv(paths["csv"], index=False, encoding="utf-8-sig")
    out.to_json(paths["jsonl"], orient="records", lines=True, force_ascii=False)
    summary = out["status"].value_counts().rename_axis("status").reset_index(name="count")
    fields = ["name", "address", "pin_code", "phone_raw", "website", "latitude", "longitude", "hours_raw"]
    cov = pd.DataFrame({"field": fields, "filled": [int(out[f].notna().sum() - (out[f] == "").sum()) for f in fields],
                        "total": len(out)})
    with pd.ExcelWriter(paths["xlsx"], engine="openpyxl") as xw:
        out.to_excel(xw, sheet_name="listings", index=False)
        summary.to_excel(xw, sheet_name="status_summary", index=False)
        cov.to_excel(xw, sheet_name="field_coverage", index=False)
        ws = xw.sheets["listings"]
        ws.freeze_panes = "A2"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(50, max(12, len(str(col[0].value)) + 2))
    return paths
