"""Dedup key construction.

A report's identity is *who reported which event through which channel*:
the same source + reporter + event always collapses to one stored report,
regardless of network retries.
"""

from __future__ import annotations

import hashlib


def report_dedup_key(source: str, reporter_id: str, event_key: str) -> str:
    raw = f"{source}|{reporter_id}|{event_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"rpt_{digest[:32]}"
