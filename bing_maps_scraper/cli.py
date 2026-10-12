from __future__ import annotations

import argparse
from pathlib import Path

from . import fetchers, pipeline


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bing_maps_scraper", description="Scrape Bing / Bing Maps listings")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, h in (("scrape", "fetch pages (cached) and extract listings"),
                    ("reparse", "re-run extraction on cached pages only, no network")):
        p = sub.add_parser(name, help=h)
        p.add_argument("input", type=Path, help="CSV/XLSX with a 'url' column, or .txt with one URL per line")
        p.add_argument("-o", "--out", type=Path, default=Path("output"))
        p.add_argument("--country", default="IN", choices=["IN", "US", "GB"], help="PIN/postcode pattern")
        p.add_argument("--workers", type=int, default=4)
        if name == "scrape":
            p.add_argument("--fetcher", default="auto", choices=["auto", "firecrawl", "playwright", "http"])
            p.add_argument("--wait-ms", type=int, default=5000, help="JS render wait")
            p.add_argument("--llm-extract", action="store_true", help="also ask Firecrawl for LLM extraction (extra credits); used only as last-resort fallback")
            p.add_argument("--refetch", action="store_true", help="ignore cache")
            p.add_argument("--delay", type=float, default=1.0, help="max random jitter (s) before each request")
    a = ap.parse_args(argv)

    df = pipeline.read_inputs(a.input)
    cache = a.out / "raw_cache"
    fetcher = None
    if a.cmd == "scrape":
        fetcher = fetchers.build(a.fetcher, wait_ms=a.wait_ms, llm_extract=a.llm_extract)
    out = pipeline.run(df, fetcher, cache, a.country, a.workers, getattr(a, "refetch", False), getattr(a, "delay", 0))
    if hasattr(fetcher, "close"):
        fetcher.close()
    paths = pipeline.write(out, a.out)
    print(out["status"].value_counts().to_string())
    print("wrote:", ", ".join(str(p) for p in paths.values()))
    return 0
