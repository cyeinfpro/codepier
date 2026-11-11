"""Small private records bound to caller, project mapping and managed workspace."""

from __future__ import annotations
import json, time, re, math
from shared.util import DevError


TERMINAL_STATES = {
    "browser": ("closed",),
    "workspace": ("removed",),
    "validation": ("passed", "failed", "stale", "unverified", "interrupted"),
}
_TERMINAL_SQL = " OR ".join(
    f"(kind='{kind}' AND json_extract(body,'$.state') IN ({','.join(repr(state) for state in states)}))"
    for kind, states in TERMINAL_STATES.items()
)
# Unknown or damaged evidence still reserves capacity until explicitly resolved.
ACTIVE_RECORD_SQL = (
    f"CASE WHEN json_valid(body) THEN CASE WHEN {_TERMINAL_SQL} THEN 0 ELSE 1 END ELSE 1 END = 1"
)
LIST_PRIORITY_SQL = f"CASE WHEN {ACTIVE_RECORD_SQL} THEN 0 ELSE 1 END"


def binding(project):
    owner = project.get("_integration_owner") or project.get("_coding_owner")
    if not isinstance(owner, str) or not owner:
        raise DevError("INTEGRATION_OWNER_REQUIRED", "缺少经过认证的调用方", 403)
    return {
        "project": project["id"],
        "root": project.get("_original_root", project["root"]),
        "workspace": project.get("_workspace_id", ""),
        "device": project.get("_coding_device", ""),
        "owner": owner,
    }


class Records:
    def __init__(self, journal):
        self.journal = journal
        with journal.lock, journal.db:
            journal.db.execute(
                "CREATE TABLE IF NOT EXISTS integration_records (kind TEXT NOT NULL,id TEXT NOT NULL,binding TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(kind,id))"
            )
            # These derived indexes were superseded by scoped ordered indexes.
            # Removing them avoids maintaining duplicate B-trees on every save.
            for obsolete in (
                "integration_record_time",
                "integration_record_binding",
                "integration_record_scope",
            ):
                journal.db.execute("DROP INDEX IF EXISTS " + obsolete)
            journal.db.execute(f"""CREATE INDEX IF NOT EXISTS integration_record_active_scope ON integration_records(
                kind,json_extract(binding,'$.project'),json_extract(binding,'$.root'),
                json_extract(binding,'$.workspace'),json_extract(binding,'$.device'),json_extract(binding,'$.owner'))
                WHERE json_valid(binding) AND {ACTIVE_RECORD_SQL}""")
            for suffix, owner in (("owner", "json_extract(binding,'$.owner'),"), ("project", "")):
                journal.db.execute(f"""CREATE INDEX IF NOT EXISTS integration_record_list_{suffix}
                    ON integration_records(kind,json_extract(binding,'$.project'),json_extract(binding,'$.root'),
                    json_extract(binding,'$.workspace'),json_extract(binding,'$.device'),
                    {owner}{LIST_PRIORITY_SQL},created DESC,id DESC) WHERE json_valid(binding)""")

    def save(self, kind, identifier, project, body, *, replace=False):
        if not re.fullmatch("[a-f0-9]{32}", identifier):
            raise DevError("INVALID_RECORD", "无效记录编号")
        bound = binding(project)
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode()) > 4 * 1024 * 1024:
            raise DevError("RECORD_TOO_LARGE", "证据记录超过 4 MiB，未保存假完整结果")
        j = self.journal
        with j.lock, j.db:
            old = j.db.execute(
                "SELECT binding FROM integration_records WHERE kind=? AND id=?", (kind, identifier)
            ).fetchone()
            if old:
                actual = json.loads(old["binding"])
                if actual != bound and not (
                    project.get("_integration_admin")
                    and {k: v for k, v in actual.items() if k != "owner"}
                    == {k: v for k, v in bound.items() if k != "owner"}
                ):
                    raise DevError("RECORD_NOT_FOUND", "记录不属于当前项目和授权", 404)
                if not replace:
                    raise DevError("RECORD_EXISTS", "记录已存在；读取原操作，不要重复执行", 409)
                j.db.execute(
                    "UPDATE integration_records SET body=? WHERE kind=? AND id=?",
                    (text, kind, identifier),
                )
            else:
                # Historical evidence is not an active resource reservation.
                # Keep receipts readable; isolate admission by authenticated
                # owner/project/workspace and resource kind, not the whole node.
                fields = ("project", "root", "workspace", "device", "owner")
                predicates = " AND ".join(
                    f"json_extract(binding,'$.{field}') IS ?" for field in fields
                )
                rows = j.db.execute(
                    f"SELECT binding FROM integration_records WHERE kind=? AND json_valid(binding) "
                    f"AND {predicates} AND {ACTIVE_RECORD_SQL}",
                    (kind, *(bound[field] for field in fields)),
                )
                count = 0
                try:
                    for row in rows:
                        # SQL narrows candidates, but exact identity stays authoritative.
                        if json.loads(row["binding"]) == bound:
                            count += 1
                            if count >= 2000:
                                raise DevError(
                                    "RECORD_QUOTA",
                                    "当前项目授权的此类活跃记录达到上限；请先结束活跃操作，历史证据仍保留",
                                )
                finally:
                    rows.close()
                j.db.execute(
                    "INSERT INTO integration_records VALUES(?,?,?,?,?)",
                    (kind, identifier, json.dumps(bound, sort_keys=True), text, time.time()),
                )
        return body

    def load(self, kind, identifier, project):
        j = self.journal
        with j.lock:
            row = j.db.execute(
                "SELECT * FROM integration_records WHERE kind=? AND id=?", (kind, identifier)
            ).fetchone()
        if not row:
            raise DevError("RECORD_NOT_FOUND", "记录不存在", 404)
        actual = json.loads(row["binding"])
        expected = binding(project)
        if actual != expected and not (
            project.get("_integration_admin")
            and {k: v for k, v in actual.items() if k != "owner"}
            == {k: v for k, v in expected.items() if k != "owner"}
        ):
            raise DevError("RECORD_NOT_FOUND", "记录不属于当前项目映射和授权", 404)
        try:
            value = json.loads(row["body"])
            if not isinstance(value, dict):
                raise ValueError("record is not an object")
            return value
        except (ValueError, TypeError) as exc:
            raise DevError(
                "RECORD_INVALID",
                "记录内容损坏，已保留原数据；请核对原操作。",
                409,
                record_id=identifier,
            ) from exc

    def project_record_ids(self, kind, project):
        """Internal cleanup candidates; load each canonical ID independently."""
        if not project.get("_integration_admin"):
            raise DevError("INTEGRATION_FORBIDDEN", "整项目清理需要本机主理人权限", 403)
        expected = binding(project)
        result = []
        # Legacy closed receipts need no RPC. Keep explicitly unconfirmed and
        # damaged records eligible, without decoding historical payloads in Python.
        pending = (
            """ AND CASE WHEN json_valid(body) THEN CASE
            WHEN json_extract(body,'$.state')='closed'
                 AND json_type(body,'$.tab_cleanup_confirmed') IS NOT 'false'
            THEN 0 ELSE 1 END ELSE 1 END = 1"""
            if kind == "browser"
            else ""
        )
        with self.journal.lock:
            # Cleanup spans owners/workspaces only within this exact mapping.
            # Unrelated or unidentifiable records must not stall project stop.
            rows = self.journal.db.execute(
                "SELECT binding,id FROM integration_records WHERE kind=? "
                "AND json_valid(binding) AND json_extract(binding,'$.project') IS ? "
                "AND json_extract(binding,'$.root') IS ? AND json_extract(binding,'$.device') IS ?"
                + pending,
                (kind, expected["project"], expected["root"], expected["device"]),
            ).fetchall()
        for row in rows:
            actual = json.loads(row["binding"])
            if all(actual.get(k) == expected.get(k) for k in ("project", "root", "device")):
                bound = {
                    **project,
                    "_workspace_id": actual.get("workspace"),
                    "_integration_owner": actual.get("owner"),
                }
                result.append((bound, row["id"]))
        return result

    @staticmethod
    def summary(kind, identifier, body):
        """Validate list fields before consumers render them or offer actions."""
        value = json.loads(body)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("state"), str)
            or not value["state"]
        ):
            raise ValueError("invalid record state")
        id_field = {
            "workspace": "workspace_id",
            "browser": "lease_id",
            "validation": "validation_id",
        }.get(kind)
        if id_field:
            if value.get(id_field, identifier) != identifier:
                raise ValueError("record identity mismatch")
            value = {**value, id_field: identifier}
        if kind == "workspace" and (not isinstance(value.get("path"), str) or not value["path"]):
            raise ValueError("invalid workspace path")
        if kind in {"browser", "validation"}:
            number = value.get("expires" if kind == "browser" else "created")
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
            ):
                raise ValueError("invalid record timestamp")
        if kind == "validation" and not isinstance(value.get("label"), str):
            raise ValueError("invalid validation label")
        return value

    def list_page(self, kind, project, limit=100):
        j = self.journal
        bound = binding(project)
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise DevError("INVALID_LIMIT", "记录数量必须在 1 至 2000 之间")
        fields = ("project", "root", "workspace", "device") + (
            () if project.get("_integration_admin") else ("owner",)
        )
        predicates = " AND ".join(f"json_extract(binding,'$.{field}') IS ?" for field in fields)
        result = []
        errors = []
        with j.lock:
            # Both owner and administrator orders have matching indexes. Legacy
            # JSON key ordering stays valid; exact identity remains authoritative.
            rows = j.db.execute(
                f"SELECT *,{ACTIVE_RECORD_SQL} AS record_active FROM integration_records WHERE kind=? AND json_valid(binding) "
                f"AND {predicates} ORDER BY {LIST_PRIORITY_SQL},created DESC,id DESC",
                (kind, *(bound[field] for field in fields)),
            )
            try:
                for row in rows:
                    actual = json.loads(row["binding"])
                    same = (
                        actual == bound
                        or project.get("_integration_admin")
                        and {k: v for k, v in actual.items() if k != "owner"}
                        == {k: v for k, v in bound.items() if k != "owner"}
                    )
                    if not same:
                        continue
                    try:
                        result.append(self.summary(kind, row["id"], row["body"]))
                    except (ValueError, TypeError, OverflowError):
                        errors.append(
                            {
                                "record_id": row["id"],
                                "kind": kind,
                                "code": "RECORD_INVALID",
                                "active": bool(row["record_active"]),
                                "message": "记录内容损坏，已保留原数据；请核对原操作。",
                            }
                        )
                    if len(result) + len(errors) >= limit:
                        break
            finally:
                rows.close()
        return {"records": result, "record_errors": errors}
