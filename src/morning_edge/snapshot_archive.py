"""Bounded decoded-payload cache for read-only archive consumers."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
import json
import sqlite3
from typing import Any, Callable, Mapping


class ArchivePayloads:
    def __init__(self, connection: sqlite3.Connection, *, max_bytes: int = 8 * 1024 * 1024):
        self.connection = connection
        self.max_bytes = max_bytes
        self.cache: OrderedDict[str, tuple[Any, int]] = OrderedDict()
        self.cached_bytes = 0
        self.dates: dict[int, date] = {}

    def load(self, key: str) -> Any:
        if key in self.cache:
            value = self.cache.pop(key)
            self.cache[key] = value
            return value[0]
        row = self.connection.execute(
            "SELECT content_json FROM raw_payloads WHERE content_hash=?", (key,)
        ).fetchone()
        if row is None:
            raise ValueError("archive payload is missing")
        text = row[0]
        payload = json.loads(text)
        size = len(text.encode("utf-8"))
        while self.cache and self.cached_bytes + size > self.max_bytes:
            _, (_, removed) = self.cache.popitem(last=False)
            self.cached_bytes -= removed
        # An oversized payload is usable, but must not remain in the cache.
        if size <= self.max_bytes:
            self.cache[key] = (payload, size)
            self.cached_bytes += size
        return payload


@dataclass(frozen=True, slots=True)
class ArchiveSnapshot:
    snapshot_id: int
    dataset: str
    as_of: str
    retrieved_at: str
    metadata: Mapping[str, Any]
    content_hash: str
    archive: ArchivePayloads
    date_selector: Callable[[Mapping[str, Any], Any, str, str], date]

    @property
    def payload(self) -> Any:
        return self.archive.load(self.content_hash)

    @property
    def market_date(self) -> date:
        if self.snapshot_id not in self.archive.dates:
            requested = self.metadata.get("requested_market_date")
            if isinstance(requested, str):
                try:
                    self.archive.dates[self.snapshot_id] = date.fromisoformat(requested)
                    return self.archive.dates[self.snapshot_id]
                except ValueError:
                    pass
            self.archive.dates[self.snapshot_id] = self.date_selector(
                self.metadata, self.payload, self.as_of, self.dataset
            )
        return self.archive.dates[self.snapshot_id]
