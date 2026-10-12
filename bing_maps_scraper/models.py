from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


@dataclass
class Page:
    """Raw result of fetching one URL."""
    url: str
    html: str = ""
    markdown: str = ""
    status_code: int | None = None
    fetcher: str = ""
    final_url: str = ""
    llm_json: dict | None = None  # optional Firecrawl structured extraction
    error: str = ""


@dataclass
class Listing:
    input_url: str
    url_kind: str = ""
    ypid: str = ""
    name: str = ""
    address: str = ""
    pin_code: str = ""
    phone_raw: str = ""
    phone_digits: str = ""
    website: str = ""
    website_domain: str = ""
    latitude: float | None = None
    longitude: float | None = None
    hours_raw: str = ""
    hours_json: str = ""  # {"Mon": "09:00-18:00", ...}
    status: str = "failed"  # ok | partial | blocked | failed
    error: str = ""
    fetcher: str = ""
    field_sources: dict = field(default_factory=dict)  # field -> parser layer
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_row(self) -> dict:
        row = asdict(self)
        row["field_sources"] = json.dumps(self.field_sources, sort_keys=True)
        return row
