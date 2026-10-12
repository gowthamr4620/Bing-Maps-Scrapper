"""Page fetchers. Firecrawl is primary; Playwright / plain HTTP are fallbacks.

Firecrawl is called through its REST API (a script cannot call an MCP connector),
using the same account: set FIRECRAWL_API_KEY.
"""
from __future__ import annotations

import glob
import os
import threading
import time

import requests

from .models import Page
from .urls import fetch_url

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "address": {"type": "string"}, "postal_code": {"type": "string"},
        "phone": {"type": "string"}, "website": {"type": "string"},
        "opening_hours": {"type": "string"},
        "latitude": {"type": "number"}, "longitude": {"type": "number"},
    },
}


class Fetcher:
    name = "base"

    def fetch(self, url: str) -> Page:  # pragma: no cover
        raise NotImplementedError


class FirecrawlFetcher(Fetcher):
    name = "firecrawl"

    def __init__(self, api_key: str | None = None, api_url: str | None = None,
                 wait_ms: int = 5000, llm_extract: bool = False, timeout: int = 120, retries: int = 3):
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("FIRECRAWL_API_KEY is not set")
        self.endpoint = (api_url or os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev")).rstrip("/") + "/v1/scrape"
        self.wait_ms, self.llm_extract, self.timeout, self.retries = wait_ms, llm_extract, timeout, retries

    def fetch(self, url: str) -> Page:
        formats = ["rawHtml", "markdown"] + (["json"] if self.llm_extract else [])
        body = {"url": fetch_url(url), "formats": formats, "onlyMainContent": False, "waitFor": self.wait_ms,
                "location": {"country": "IN", "languages": ["en-IN", "en"]}}  # Bing localises (we got Hindi); pin English
        if self.llm_extract:
            body["jsonOptions"] = {"schema": LLM_SCHEMA,
                                   "prompt": "Extract the business listing shown on this Bing page. Return only what is visible."}
        err = ""
        for attempt in range(self.retries):
            try:
                r = requests.post(self.endpoint, json=body, timeout=self.timeout,
                                  headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code in (429, 500, 502, 503, 504):
                    err = f"HTTP {r.status_code}"
                    time.sleep(2 ** attempt * 2)
                    continue
                payload = r.json()
                if not r.ok or not payload.get("success", False):
                    return Page(url, fetcher=self.name, status_code=r.status_code,
                                error=str(payload.get("error") or payload)[:300])
                d = payload.get("data", {})
                meta = d.get("metadata", {})
                return Page(url, html=d.get("rawHtml") or d.get("html") or "", markdown=d.get("markdown") or "",
                            status_code=meta.get("statusCode"), fetcher=self.name,
                            final_url=meta.get("sourceURL") or meta.get("url") or url, llm_json=d.get("json"))
            except (requests.RequestException, ValueError) as e:
                err = f"{type(e).__name__}: {e}"
                time.sleep(2 ** attempt * 2)
        return Page(url, fetcher=self.name, error=err or "unknown firecrawl failure")


class HttpFetcher(Fetcher):
    """Plain GET. Bing Maps is JS-rendered, so expect partial data; fine for /search local cards."""
    name = "http"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def fetch(self, url: str) -> Page:
        try:
            r = requests.get(fetch_url(url), timeout=self.timeout, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
            return Page(url, html=r.text, status_code=r.status_code, fetcher=self.name, final_url=r.url)
        except requests.RequestException as e:
            return Page(url, fetcher=self.name, error=f"{type(e).__name__}: {e}")


class PlaywrightFetcher(Fetcher):
    """Headless Chromium. Captures Bing's `overlaybfpr` XHR, which carries the structured
    entity record (title, address, phone, website, exact geometry) for the listing.
    Thread-safe: each worker thread owns its own browser."""
    name = "playwright"

    def __init__(self, wait_ms: int = 20000, headless: bool = True, executable_path: str | None = None):
        self.wait_ms, self.headless = wait_ms, headless
        self.exe = executable_path or os.environ.get("CHROMIUM_PATH") or next(
            iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))), None)
        self._local, self._open, self._lock = threading.local(), [], threading.Lock()

    def _context(self):
        if not hasattr(self._local, "ctx"):
            from playwright.sync_api import sync_playwright  # lazy: optional dependency
            pw = sync_playwright().start()
            kw = {"headless": self.headless, "args": ["--no-sandbox"]}
            if self.exe:
                kw["executable_path"] = self.exe
            browser = pw.chromium.launch(**kw)
            self._local.ctx = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
            with self._lock:
                self._open.append((pw, browser))
        return self._local.ctx

    def fetch(self, url: str) -> Page:
        target, err = fetch_url(url), ""
        for attempt in range(2):
            page = self._context().new_page()
            captured: dict[str, str] = {}

            def on_response(r, captured=captured):
                if "overlaybfpr" in r.url:
                    try:
                        captured["overlaybfpr"] = r.text()
                    except Exception:  # noqa: BLE001 - body can be gone if page closed
                        pass

            page.on("response", on_response)
            try:
                resp = page.goto(target, wait_until="domcontentloaded", timeout=60000)
                waited = 0
                while "overlaybfpr" not in captured and waited < self.wait_ms:
                    page.wait_for_timeout(500)
                    waited += 500
                page.wait_for_timeout(1500)  # let the panel render (hours etc.)
                if captured or attempt == 1:
                    return Page(url, html=page.content(), status_code=resp.status if resp else None,
                                fetcher=self.name, final_url=page.url, xhr=captured,
                                error="" if captured else "Bing returned no entity data (listing removed / invalid ypid / throttled)")
            except Exception as e:  # noqa: BLE001 - playwright raises many types
                err = f"{type(e).__name__}: {e}"[:300]
            finally:
                page.close()
            time.sleep(2)
        return Page(url, fetcher=self.name, error=err or "fetch failed")

    def close(self):
        for pw, browser in self._open:
            try:
                browser.close()
                pw.stop()
            except Exception:  # noqa: BLE001
                pass


def build(kind: str, **kw) -> Fetcher:
    if kind == "auto":
        kind = "firecrawl" if os.environ.get("FIRECRAWL_API_KEY") else "playwright"
    if kind == "firecrawl":
        return FirecrawlFetcher(wait_ms=kw.get("wait_ms", 5000), llm_extract=kw.get("llm_extract", False))
    if kind == "playwright":
        return PlaywrightFetcher(wait_ms=kw.get("wait_ms", 20000))
    if kind == "http":
        return HttpFetcher()
    raise ValueError(f"unknown fetcher {kind!r}")
