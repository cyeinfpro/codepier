"""Sanitized deterministic fixtures shared by context UI checks."""
from collections import defaultdict

from hub.context_usage import context_usage
from shared.token_estimate import summarize, usage


def synthetic_dashboard(percent=90, model="gpt-6-astra", authority="context-fixture"):
    def metric(n):
        return dict(state="available", estimated_tokens=n, low=n, high=n,
                    characters=n, utf8_bytes=n, source_truncated=False, truncated=False)
    rows = [{"id": index+1, "started": index*60, "service_ms": 100,
             "tool": "read", "project": "synthetic-project", "window_key": "synthetic-window",
             "token_usage": usage(metric(100), metric(1000))} for index in range(10)]
    summary = summarize(rows, cache_read_percent=percent, reference_model=model)
    summary["context_estimate"], context_buckets = context_usage(
        rows, 0, 3600, lambda timestamp: int(timestamp // 3600)*3600,
        cache_read_percent=percent, reference_model=model)
    buckets = defaultdict(list)
    for row in rows:
        buckets[int(row["started"] // 3600)*3600].append(row)
    trend = [{"started": key, **summarize(items, cache_read_percent=percent, reference_model=model),
              "context_estimate": context_buckets[key]} for key, items in buckets.items()]
    return {"schema_version": 3, "authority_key": authority, "summary": summary,
            "period": {"key": "today", "start": 0, "end": 3600, "timezone": "UTC", "bucket": "hour"},
            "filters": {"project": "", "connection": "", "session": ""},
            "options": {"projects": [], "connections": [], "sessions": []}, "trend": trend,
            "coverage": {"activity_row_limit": 10000, "estimate_retention_days": 30,
                         "complete_history": False}}
