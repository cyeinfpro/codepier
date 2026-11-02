"""Current-authority MCP text summaries plus bounded context scenarios."""
from __future__ import annotations

import asyncio
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Literal

from hub import iam
from hub.context_usage import context_usage
from hub.mcp_usage import MAX_ROWS, RETENTION_SECONDS, activity_actor_scope
from hub.principal import refresh_principal
from hub.token_cache_schema import revision
from shared.context_estimate import VERSION as CONTEXT_VERSION
from shared.call_estimate import VERSION as CALL_VERSION
from shared.token_estimate import summarize, NUMBERS, VERSION as TEXT_VERSION, unavailable
from shared.token_cost import DEFAULT_CACHE_READ_PERCENT, DEFAULT_REFERENCE_MODEL, MODELS, VERSION as PRICE_VERSION, pricing
from shared.util import DevError

Period = Literal["today", "7d", "30d"]
CACHE_SECONDS = 30
CACHE_ENTRIES = 8
COMPUTE_WORKERS = 2
MAX_INFLIGHT = 8


def _safe_usage(value):
    if not isinstance(value, dict) or value.get("version") != TEXT_VERSION or value.get("kind") != "estimate" or value.get("scope") != "project_tool_payload":
        raise ValueError("Invalid usage metadata")
    result = {"version": TEXT_VERSION, "kind": "estimate", "scope": "project_tool_payload", "actual_usage": None}
    for direction in ("input", "output"):
        side = value.get(direction) or unavailable()
        if side.get("state") == "unavailable":
            result[direction] = unavailable()
            continue
        if side.get("state") not in {"available", "partial"} or any(type(side.get(key)) is not int or side[key] < 0 for key in NUMBERS):
            raise ValueError("Invalid usage counters")
        if not side["low"] <= side["estimated_tokens"] <= side["high"]:
            raise ValueError("Invalid usage interval")
        result[direction] = {key: side[key] for key in NUMBERS}
        result[direction].update(state=side["state"], source_truncated=bool(side.get("source_truncated")),
                                 truncated=bool(side.get("truncated")), excluded_fields=side.get("excluded_fields", 0))
    return result


@dataclass(frozen=True)
class _Options:
    timezone: object
    period: Period = "today"
    project: str = ""
    connection: str = ""
    session: str = ""
    cache_read_percent: int = DEFAULT_CACHE_READ_PERCENT
    reference_model: str = DEFAULT_REFERENCE_MODEL
    now: float | None = None

    def validate(self):
        if self.period not in {"today", "7d", "30d"}:
            raise DevError("INVALID_PERIOD", "不支持的统计时间范围", 400)
        if not isinstance(self.reference_model, str) or self.reference_model not in MODELS:
            raise DevError("INVALID_REFERENCE_MODEL", "不支持的参考计价模型", 400)
        try:
            pricing(self.cache_read_percent, self.reference_model)
        except ValueError as error:
            raise DevError("INVALID_CACHE_ASSUMPTION", "缓存读取假设须为 0–100 的整数百分比", 400) from error


@dataclass(frozen=True)
class _Authority:
    principal: object
    projects: list
    key: str
    actor_clause: str
    actor_args: list


@dataclass(frozen=True)
class _Prepared:
    options: _Options
    authority: _Authority
    key: tuple
    mode: str
    lower: float
    upper: float
    midnight: datetime
    data_revision: int
    observed_at: float
    monotonic: float


@dataclass(frozen=True)
class _Snapshot:
    prepared: _Prepared
    rows: list
    collection_unavailable: bool


@dataclass(frozen=True)
class _CacheContext:
    # No credential, project root, actor predicate or raw activity snapshot is
    # retained by the cache after aggregation finishes.
    options: _Options
    key: tuple
    mode: str
    data_revision: int
    observed_at: float


@dataclass(frozen=True)
class _CacheEntry:
    prepared: _CacheContext
    value: dict
    expires: float
    wall_expires: float


def _authority(runtime, principal, visibility_revision):
    """Reload current authority; caller owns one short read transaction.

    A panel actor's grant ownership is part of its data filter, not merely its
    login state. Include that mapping too, so reassignment or a changed label
    cannot survive the metric refresh interval. Never cache authentication.
    """
    store = runtime.store
    principal = refresh_principal(store, principal)
    projects = runtime.list_projects(principal)
    actor_clause, actor_args = activity_actor_scope(store, principal, "a.")
    grant_filter, grant_args = "space_id=?", [principal.space_id]
    if principal.grant_id:
        grant_filter += " AND id=?"
        grant_args.append(principal.grant_id)
    elif iam.installed(store) and not principal.instance_admin:
        grant_filter += " AND user_id=?"
        grant_args.append(principal.user_id)
    elif not iam.installed(store) and not principal.admin:
        grant_filter += " AND id IS NULL"
    grants = store.all("SELECT id,user_id,space_id,label FROM grants WHERE " + grant_filter + " ORDER BY id", grant_args)
    key = hashlib.sha256(json.dumps([
        principal.user_id, principal.space_id, principal.grant_id, principal.actor,
        principal.admin, principal.instance_admin, sorted(principal.scopes),
        principal.user_epoch, principal.identity_id, principal.profile_id,
        principal.role_id, principal.authorization_mode, sorted(principal.projects),
        sorted((item["id"], item["root"], item["device_id"], item["alias"]) for item in projects),
        [(item["id"], item["user_id"], item["space_id"], item["label"]) for item in grants],
        visibility_revision,
    ], separators=(",", ":")).encode()).hexdigest()
    return _Authority(principal, projects, key, actor_clause, actor_args)


def _prepare_locked(runtime, principal, options, mode):
    data_revision, visibility_revision = revision(runtime.store)
    authority = _authority(runtime, principal, visibility_revision)
    if options.project and options.project not in {item["id"] for item in authority.projects}:
        raise DevError("PROJECT_NOT_FOUND", "未找到已授权项目", 404)
    observed_at = time.time()
    finish = datetime.fromtimestamp(options.now if options.now is not None else observed_at, options.timezone)
    midnight = finish.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days={"today": 0, "7d": 6, "30d": 29}[options.period])
    lower, upper = start.timestamp(), finish.timestamp()
    key = (mode, TEXT_VERSION, CALL_VERSION, CONTEXT_VERSION, PRICE_VERSION,
           authority.key, id(runtime.integrations.usage_metrics), str(options.timezone),
           options.period, options.project, options.connection, options.session,
           options.cache_read_percent, options.reference_model, lower, options.now)
    return _Prepared(options, authority, key, mode, lower, upper, midnight,
                     data_revision, observed_at, time.monotonic())


def _cache(runtime):
    # Accessed only while holding Store.lock. Entries are never returned to
    # callers by reference; their JSON-sized aggregate values are immutable here.
    cache = getattr(runtime, "_token_dashboard_cache", None)
    if cache is None:
        cache = runtime._token_dashboard_cache = OrderedDict()
    return cache


def _alive(entry):
    return entry.expires > time.monotonic() and entry.wall_expires > time.time()


def _prepare(runtime, principal, options, mode):
    with runtime.store.transaction(immediate=False), iam.read_scope(runtime.store):
        prepared = _prepare_locked(runtime, principal, options, mode)
        cache = _cache(runtime)
        entry = cache.get(prepared.key)
        if entry and _alive(entry) and (mode == "bounded" or entry.prepared.data_revision == prepared.data_revision):
            cache.move_to_end(prepared.key)
            return prepared, entry
        return prepared, None


def _read_snapshot(runtime, principal, expected):
    """Copy only authorized bounded rows; JSON parsing/replay happens elsewhere."""
    store, options = runtime.store, expected.options
    with store.transaction(immediate=False), iam.read_scope(store):
        prepared = _prepare_locked(runtime, principal, options, expected.mode)
        if prepared.authority.key != expected.authority.key:
            raise DevError("STATISTICS_AUTHORITY_CHANGED", "统计期间授权或项目映射已变化，请重新读取", 409)
        if prepared.key != expected.key:
            raise DevError("STATISTICS_CHANGED", "统计快照已变化或过期，请重新读取", 409)
        authority = prepared.authority
        visible_projects = [item for item in authority.projects if not options.project or item["id"] == options.project]
        rows = []
        collector = runtime.integrations.usage_metrics
        collection_unavailable = collector is None
        usage_floor = prepared.observed_at - RETENTION_SECONDS
        history_floor = prepared.upper - RETENTION_SECONDS
        for offset in range(0, len(visible_projects), 100):
            batch = visible_projects[offset:offset + 100]
            mappings = " OR ".join("(a.project=? AND a.root=? AND a.device=?)" for _ in batch)
            values = [value for item in batch for value in (item["id"], item["root"], item["device_id"])]
            where = f"({mappings}) AND a.started>=? AND a.started<=? AND ({authority.actor_clause})"
            where += " AND (a.grant_id IS NULL OR EXISTS (SELECT 1 FROM grants sg WHERE sg.id=a.grant_id AND sg.space_id=?))"
            values.extend((history_floor, prepared.upper, *authority.actor_args, authority.principal.space_id))
            columns = ("a.id,a.project,a.root,a.device,a.grant_id,a.actor,a.window_key,a.started,"
                       "a.tool,a.service_ms,a.operation_id,g.label AS connection_label")
            source = " FROM mcp_activity a LEFT JOIN grants g ON g.id=a.grant_id "
            if collector is not None:
                try:
                    items = store.all("SELECT " + columns + ",u.created AS usage_created,u.metrics AS usage_json" +
                                      source + "LEFT JOIN mcp_usage u ON u.activity_id=a.id AND u.created>=? WHERE " +
                                      where + " ORDER BY a.id DESC LIMIT ?", (usage_floor, *values, MAX_ROWS))
                except sqlite3.Error:
                    collection_unavailable = True
                    items = store.all("SELECT " + columns + source + "WHERE " + where +
                                      " ORDER BY a.id DESC LIMIT ?", (*values, MAX_ROWS))
            else:
                items = store.all("SELECT " + columns + source + "WHERE " + where +
                                  " ORDER BY a.id DESC LIMIT ?", (*values, MAX_ROWS))
            rows.extend(items)
            # Bound memory across arbitrarily many project batches too.
            rows = sorted(rows, key=lambda row: row["id"], reverse=True)[:MAX_ROWS]
        return _Snapshot(prepared, rows, collection_unavailable)


def _aggregate(snapshot):
    """Pure bounded computation: no Store, connection, request or IAM scope."""
    prepared = snapshot.prepared
    options, authority = prepared.options, prepared.authority
    lower, upper = prepared.lower, prepared.upper
    rows = sorted({row["id"]: row for row in snapshot.rows}.values(), key=lambda row: row["id"], reverse=True)[:MAX_ROWS]
    collection_unavailable = snapshot.collection_unavailable
    expiry_seconds = min(CACHE_SECONDS, (prepared.midnight + timedelta(days=1)).timestamp() - upper)
    if options.now is None:
        for row in rows:
            expiry_seconds = min(expiry_seconds, max(0, row["started"] + RETENTION_SECONDS - upper))
    # Prior rows warm the context replay, but cannot add labels outside the
    # selected period. Retention also bounds the lifetime of these options.
    period_rows = [row for row in rows if row["started"] >= lower]
    connections = {row["grant_id"] or "panel": row.get("connection_label") or "管理面板" for row in period_rows}
    if options.connection:
        rows = [row for row in rows if (row["grant_id"] or "panel") == options.connection]
        period_rows = [row for row in period_rows if (row["grant_id"] or "panel") == options.connection]
    windows = sorted({row["window_key"] for row in period_rows if row.get("window_key")})
    if options.session:
        rows = [row for row in rows if (row.get("window_key") or "uncorrelated") == options.session]
    for row in rows:
        row["token_usage"] = None
        encoded = row.pop("usage_json", None)
        if encoded:
            try:
                row["token_usage"] = _safe_usage(json.loads(encoded))
            except (AttributeError, TypeError, ValueError):
                collection_unavailable = True
        if row.get("usage_created") is not None:
            expiry_seconds = min(expiry_seconds, max(0, row["usage_created"] + RETENTION_SECONDS - prepared.observed_at))
    period_rows = [row for row in rows if row["started"] >= lower]
    total = summarize(period_rows, cache_read_percent=options.cache_read_percent, reference_model=options.reference_model)
    total["scope"] = "retained_authorized_project_activity"

    def bucket_for(timestamp):
        local = datetime.fromtimestamp(timestamp, options.timezone)
        if options.period == "today":
            return lower + int((timestamp - lower) // 3600) * 3600
        return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    context, context_buckets, calls, call_buckets = context_usage(rows, lower, upper, bucket_for,
                                            cache_read_percent=options.cache_read_percent,
                                            reference_model=options.reference_model,
                                            collection_unavailable=collection_unavailable,
                                            include_call_estimate=True)
    total["context_estimate"] = context
    total["call_estimate"] = calls
    buckets = defaultdict(list)
    for row in period_rows:
        buckets[bucket_for(row["started"])].append(row)
    trend = []
    for bucket in sorted(set(buckets) | set(context_buckets) | set(call_buckets)):
        point = summarize(buckets[bucket], cache_read_percent=options.cache_read_percent, reference_model=options.reference_model)
        point["scope"] = total["scope"]
        point["context_estimate"] = context_buckets.get(bucket)
        point["call_estimate"] = call_buckets.get(bucket)
        trend.append({"started": bucket, **point})
    result = {
        "schema_version": 4, "authority_key": authority.key,
        "period": {"key": options.period, "start": lower, "end": upper,
                   "timezone": str(options.timezone), "bucket": "hour" if options.period == "today" else "day"},
        "filters": {"project": options.project, "connection": options.connection, "session": options.session},
        "options": {
            "projects": [{"id": item["id"], "label": item["alias"]} for item in authority.projects],
            "connections": [{"id": key, "label": label} for key, label in sorted(connections.items())],
            "sessions": [{"id": key, "label": f"匿名窗口 {index + 1}"} for index, key in enumerate(windows)] +
                [{"id": "uncorrelated", "label": "未关联窗口"}],
        },
        "summary": total, "trend": trend,
        "freshness": {"as_of": prepared.observed_at, "max_cache_age_seconds": CACHE_SECONDS,
                      "policy": "bounded_snapshot" if prepared.mode == "bounded" else "revision_checked"},
        "coverage": {"scope": total["scope"], "complete_history": False,
            "activity_row_limit": MAX_ROWS, "estimate_retention_days": RETENTION_SECONDS // 86400,
            "collection_unavailable": collection_unavailable,
            "oldest_visible_activity": min((row["started"] for row in period_rows), default=None),
            "context_warmup_attempts": len(rows) - len(period_rows),
            "context_version": CONTEXT_VERSION, "call_estimate_version": CALL_VERSION},
    }
    cached = _CacheContext(options, prepared.key, prepared.mode, prepared.data_revision, prepared.observed_at)
    return _CacheEntry(cached, result, prepared.monotonic + expiry_seconds, prepared.observed_at + expiry_seconds)


def _remember(runtime, entry):
    with runtime.store.lock:
        cache = _cache(runtime)
        if not entry.value["coverage"]["collection_unavailable"] and _alive(entry):
            cache[entry.prepared.key] = entry
            cache.move_to_end(entry.prepared.key)
        for key in list(cache):
            if not _alive(cache[key]):
                cache.pop(key, None)
        while len(cache) > CACHE_ENTRIES:
            cache.popitem(last=False)


def _check(runtime, principal, entry):
    # A scope never crosses an await. Validate both the originating credential
    # and its complete current data filter after computation or a cache hit.
    with runtime.store.transaction(immediate=False), iam.read_scope(runtime.store):
        current = _prepare_locked(runtime, principal, entry.prepared.options, entry.prepared.mode)
        if current.authority.key != entry.value["authority_key"]:
            raise DevError("STATISTICS_AUTHORITY_CHANGED", "统计期间授权或项目映射已变化，请重新读取", 409)
        if (current.key != entry.prepared.key or
                (entry.prepared.mode == "revision" and current.data_revision != entry.prepared.data_revision) or
                not _alive(entry)):
            raise DevError("STATISTICS_CHANGED", "统计快照已变化或过期，请重新读取", 409)


def validate_dashboard_snapshot(runtime, principal, snapshot):
    """Revalidate authority and both expiry clocks after a further async wait."""
    _check(runtime, principal, snapshot)


def _public(entry):
    value = deepcopy(entry.value)
    value["freshness"]["age_seconds"] = max(0, time.time() - entry.prepared.observed_at)
    return value


def token_dashboard(runtime, principal, timezone, *, period: Period = "today",
                    project: str = "", connection: str = "", session: str = "",
                    cache_read_percent: int = DEFAULT_CACHE_READ_PERCENT,
                    reference_model: str = DEFAULT_REFERENCE_MODEL,
                    now: float | None = None):
    """Synchronous exact-revision helper; HTTP uses token_dashboard_async.

    Parsing, replay and deep copies are outside this helper's database scope.
    Trusted synchronous callers must not hold an outer Store.lock if they need
    other database work to proceed while they compute.
    """
    options = _Options(timezone, period, project, connection, session, cache_read_percent, reference_model, now)
    options.validate()
    prepared, entry = _prepare(runtime, principal, options, "revision")
    if entry is None:
        entry = _aggregate(_read_snapshot(runtime, principal, prepared))
        _check(runtime, principal, entry)
        _remember(runtime, entry)
    else:
        _check(runtime, principal, entry)
    return _public(entry)


class _DashboardService:
    """Loop-owned bounded singleflight; the executor never receives DB access."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.loop = asyncio.get_running_loop()
        self.executor = ThreadPoolExecutor(max_workers=COMPUTE_WORKERS, thread_name_prefix="hub-token")
        self.slots = asyncio.Semaphore(COMPUTE_WORKERS)
        self.inflight = {}
        self.closed = False
        self.computed = 0
        self.coalesced = 0

    async def produce(self, principal, prepared):
        # Waiting keys retain only their filter/authority, never a 10,000-row
        # snapshot. Thus at most COMPUTE_WORKERS large snapshots exist at once.
        async with self.slots:
            snapshot = await self.runtime.store.run(_read_snapshot, self.runtime, principal, prepared)
            entry = await self.loop.run_in_executor(self.executor, _aggregate, snapshot)
            self.computed += 1
            await self.runtime.store.run(_remember, self.runtime, entry)
            return entry

    def finished(self, key, task):
        if self.inflight.get(key) is task:
            self.inflight.pop(key, None)
        # If all HTTP waiters disconnected, still retrieve a background failure.
        if not task.cancelled():
            task.exception()

    async def get(self, principal, prepared):
        if self.closed or getattr(self.runtime, "stopping", False):
            raise DevError("STATISTICS_UNAVAILABLE", "统计服务正在关闭", 503)
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Dashboard service belongs to another event loop")
        task = self.inflight.get(prepared.key)
        if task is None:
            if len(self.inflight) >= MAX_INFLIGHT:
                raise DevError("STATISTICS_BUSY", "统计计算已达到并发上限，请稍后重试", 503)
            task = self.loop.create_task(self.produce(principal, prepared))
            self.inflight[prepared.key] = task
            task.add_done_callback(lambda done: self.finished(prepared.key, done))
        else:
            self.coalesced += 1
        # One cancelled reader cannot cancel work shared by other authorized
        # readers. The per-runtime bound also limits detached work.
        return await asyncio.shield(task)

    async def close(self):
        self.closed = True
        tasks = list(self.inflight.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=False)


async def token_dashboard_async(runtime, principal, timezone, *, period: Period = "today",
                                project: str = "", connection: str = "", session: str = "",
                                cache_read_percent: int = DEFAULT_CACHE_READ_PERCENT,
                                reference_model: str = DEFAULT_REFERENCE_MODEL,
                                now: float | None = None):
    """Current-authority HTTP statistics with an explicit bounded data age.

    Frequent activity writes do not invalidate this 30-second snapshot. Every
    caller is reauthorized both before sharing computation and after waiting;
    visibility changes bypass the refresh interval. Late metrics and deletions
    are observed on the next refresh even with continuous background writes.
    """
    options = _Options(timezone, period, project, connection, session, cache_read_percent, reference_model, now)
    return _public(await _dashboard_entry_async(runtime, principal, options))


async def overview_token_snapshot(runtime, principal, timezone):
    """Carry private validity metadata through overview's final DB phase."""
    entry = await _dashboard_entry_async(runtime, principal, _Options(timezone))
    return _public(entry), entry


async def _dashboard_entry_async(runtime, principal, options):
    options.validate()
    service = getattr(runtime, "_token_dashboard_service", None)
    if getattr(runtime, "stopping", False) or (service is not None and service.closed):
        raise DevError("STATISTICS_UNAVAILABLE", "统计服务正在关闭", 503)
    prepared, entry = await runtime.store.run(_prepare, runtime, principal, options, "bounded")
    if entry is None:
        # Another request may have installed the service while this request
        # awaited its authorization phase. Select/create without another await.
        service = getattr(runtime, "_token_dashboard_service", None)
        if service is None:
            service = runtime._token_dashboard_service = _DashboardService(runtime)
        entry = await service.get(principal, prepared)
    if getattr(runtime, "stopping", False) or (service is not None and service.closed):
        raise DevError("STATISTICS_UNAVAILABLE", "统计服务正在关闭", 503)
    await runtime.store.run(_check, runtime, principal, entry)
    if getattr(runtime, "stopping", False) or (service is not None and service.closed):
        raise DevError("STATISTICS_UNAVAILABLE", "统计服务正在关闭", 503)
    return entry


async def aclose_token_dashboard(runtime):
    service = getattr(runtime, "_token_dashboard_service", None)
    if service is not None:
        # Shutdown cancellation must not let the Store close underneath an
        # already admitted snapshot or its final cache publication.
        closing = asyncio.create_task(service.close())
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        if getattr(runtime, "_token_dashboard_service", None) is service:
            del runtime._token_dashboard_service
        if cancelled:
            raise asyncio.CancelledError
