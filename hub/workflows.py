"""Durable, scoped development checklists layered above existing operations.

This module stores progress; it never executes commands or asks a model to run.
Mutations, replay receipts and audit events commit in one SQLite transaction.
"""
from __future__ import annotations
from hub.access_profiles import effective_grant
from hub.roles import role_project_scopes
from hub import iam

import base64
import json
import time
import uuid

from shared.crypto import digest
from shared.util import DevError
from shared.workflow_templates import TEMPLATES


def encode_cursor(created, identifier):
    return base64.urlsafe_b64encode(json.dumps([created, identifier]).encode()).decode()


def decode_cursor(value):
    try:
        created, identifier = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        import math
        if type(created) not in (int, float) or not math.isfinite(created) or not isinstance(identifier, str) or len(identifier) != 32:
            raise ValueError()
        return created, identifier
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise DevError("INVALID_CURSOR", "无效的工作流游标；请重新读取第一页") from exc


class Workflows:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    def load(self, identifier, principal, *, write=False):
        row = self.store.one("SELECT * FROM workflows WHERE id=?", (identifier,))
        principal = iam.require_record(self.store, principal, row, kind='WORKFLOW')
        if write and not principal.instance_admin:
            owned = row['grant_id'] == principal.grant_id if principal.grant_id else row['owner_user_id'] == principal.user_id
            if not owned:
                raise DevError('WORKFLOW_READ_ONLY', '共享工作流只授予查看；修改仍需原身份', 403)
        project = self.runtime.project(row["project_id"], principal)
        changed = row["root"] != project["root"] or row["device_id"] != project["device_id"]
        self.runtime.authorize(principal, 'write' if write else 'read', project_id=project['id'])
        if write:
            if project["mode"] != "write":
                raise DevError("READ_ONLY", "项目已设为只读；不能更改工作流", 403)
            if changed:
                raise DevError("WORKFLOW_MAPPING_CHANGED", "项目目录或设备已更改；请核对旧任务并为新映射建立工作流", 409)
        row["steps"] = json.loads(row["steps"])
        row["project_alias"] = project["alias"]
        row["mapping_changed"] = changed
        row["can_update"] = (principal.instance_admin or (row['grant_id'] == principal.grant_id if principal.grant_id else row['owner_user_id'] == principal.user_id)) and not changed and project["mode"] == "write" and "write" in role_project_scopes(self.store, principal, project["id"])
        row["assigned_to_mcp"] = row["grant_id"] is not None
        return row

    @staticmethod
    def public(row):
        data = {k: v for k, v in row.items() if k not in {"actor", "grant_id", "root", "device_id"}}
        data["workflow_id"] = row["id"]
        steps = row["steps"]
        data["progress"] = {"completed": sum(s["state"] == "completed" for s in steps),
                            "skipped": sum(s["state"] == "skipped" for s in steps), "total": len(steps)}
        data["next_step"] = next((s["id"] for s in steps if s["state"] == "running"), None) or next(
            (s["id"] for s in steps if s["state"] == "pending"), None)
        if row["state"] != "active":
            data["next_step"] = None
        data["next_action"] = "resume" if row["state"] == "blocked" else "checkpoint" if row["state"] == "active" else None
        data["execution_policy"] = "仅保存进度，不自动执行。取消工作流不会停止本机操作；需单独 operations_cancel。"
        data["evidence_policy"] = "操作成功是完成步骤的必要条件，不证明验收语义；仍须检查输出与实际行为。"
        return data

    def get(self, args, principal):
        # A consistent snapshot: a concurrent checkpoint cannot mix versions/events.
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN")
            row = self.load(args["workflow_id"], principal)
            clause, values = "workflow_id=?", [row["id"]]
            if args["before_event_id"] is not None:
                clause += " AND id<?"
                values.append(args["before_event_id"])
            events = self.store.all("SELECT id,action,summary,evidence,at,version FROM workflow_events WHERE " + clause + " ORDER BY id DESC LIMIT ?",
                                    (*values, args["event_limit"] + 1))
            more = len(events) > args["event_limit"]
            events = events[:args["event_limit"]]
            for event in events:
                event["evidence"] = json.loads(event["evidence"])
            return {**self.public(row), "events": events,
                    "next_before_event_id": events[-1]["id"] if more else None}

    def list(self, args, principal):
        principal = iam.live_principal(self.store, principal)
        clause, values = iam.private_sql(principal, 'w.')
        clauses = [clause, "EXISTS (SELECT 1 FROM projects p WHERE p.id=w.project_id)"]
        if "*" not in principal.projects:
            clauses.append("w.project_id IN (%s)" % (",".join("?" for _ in principal.projects) or "NULL"))
            values.extend(principal.projects)
        if args["project"]:
            clauses.append("w.project_id=?")
            values.append(self.runtime.project(args["project"], principal)["id"])
        if args["state"]:
            clauses.append("w.state=?")
            values.append(args["state"])
        if args["cursor"]:
            created, identifier = decode_cursor(args["cursor"])
            clauses.append("(w.created<? OR (w.created=? AND w.id<?))")
            values.extend((created, created, identifier))
        rows = self.store.all("SELECT w.id,w.project_id,w.title,w.goal,w.state,w.version,w.created,w.updated,w.steps,w.grant_id IS NOT NULL AS assigned_to_mcp,p.alias AS project_alias FROM workflows w JOIN projects p ON p.id=w.project_id WHERE " +
                              " AND ".join(clauses) + " ORDER BY w.created DESC,w.id DESC LIMIT ?", (*values, args["limit"] + 1))
        more = len(rows) > args["limit"]
        rows = rows[:args["limit"]]
        for row in rows:
            steps = json.loads(row.pop("steps"))
            row["assigned_to_mcp"] = bool(row["assigned_to_mcp"])
            row["workflow_id"] = row["id"]
            row["progress"] = {"completed": sum(s["state"] == "completed" for s in steps),
                               "skipped": sum(s["state"] == "skipped" for s in steps), "total": len(steps)}
        return {"workflows": rows, "next_cursor": encode_cursor(rows[-1]["created"], rows[-1]["id"]) if more else None,
                "templates": [{"id": k, **v} for k, v in TEMPLATES.items()]}

    def replay(self, args, principal, fingerprint):
        old = self.store.one("SELECT * FROM workflow_replays WHERE actor=? AND idem=?", (principal.actor, args["idempotency_key"]))
        if old:
            self.load(old["workflow_id"], principal, write=True)
            if old["fingerprint"] != fingerprint:
                raise DevError("IDEMPOTENCY_CONFLICT", "工作流幂等键已经用于不同请求", 409)
            return {**json.loads(old["receipt"]), "replayed": True}
        return None

    def record(self, row, args, principal, fingerprint, action, summary, evidence):
        receipt = {"workflow_id": row["id"], "version": row["version"], "state": row["state"], "replayed": False, "next": "workflows_get"}
        self.store.db.execute("INSERT INTO workflow_replays(actor,idem,fingerprint,workflow_id,receipt) VALUES (?,?,?,?,?)",
                              (principal.actor, args["idempotency_key"], fingerprint, row["id"], json.dumps(receipt)))
        self.store.db.execute("INSERT INTO workflow_events(workflow_id,action,summary,evidence,at,version) VALUES (?,?,?,?,?,?)",
                              (row["id"], action, summary, json.dumps(evidence, ensure_ascii=False), row["updated"], row["version"]))
        iam.audit(self.store, principal, 'workflows.' + action, row['id'], status=row['state'],
                  detail={'workflow_id': row['id'], 'version': row['version'], 'evidence_count': len(evidence)})
        return receipt

    def evidence(self, identifiers, row, principal, *, must_succeed=False):
        evidence = []
        for identifier in dict.fromkeys(identifiers):
            op = self.runtime.operation(identifier, principal)
            if (op["project_id"] != row["project_id"] or op["device_id"] != row["device_id"] or
                    row["grant_id"] is not None and op["grant_id"] != row["grant_id"]):
                raise DevError("EVIDENCE_SCOPE", "证据必须来自同一项目、设备和工作流授权", 403)
            if op["created"] < row["created"]:
                raise DevError("EVIDENCE_STALE", "请使用工作流建立后执行的操作，不能用历史成功冒充本轮验收", 409)
            result = op.get("result") or {}
            if must_succeed and (op["state"] != "succeeded" or not result.get("ok")):
                raise DevError("EVIDENCE_NOT_SUCCESSFUL", "步骤未完成：证据操作尚未成功；请先等待或修复失败", 409)
            data = result.get("data") or {}
            if must_succeed and (data.get("timed_out") or data.get("cancelled") or
                    "exit_code" in data and (type(data["exit_code"]) is not int or data["exit_code"] != 0)):
                raise DevError("EVIDENCE_NOT_SUCCESSFUL", "命令未成功执行；不能作为步骤完成证据", 409)
            evidence.append({"operation_id": identifier, "tool": op["tool"], "state": op["state"],
                             "exit_code": data.get("exit_code"), "updated": op["updated"]})
        if must_succeed and not evidence:
            raise DevError("EVIDENCE_REQUIRED", "完成步骤必须关联至少一个本轮成功操作，并说明验收结论", 409)
        return evidence

    def create(self, args, principal):
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            project = self.runtime.project(args["project"], principal)
            self.runtime.authorize(principal, 'write', project_id=project['id'])
            if project["mode"] != "write":
                raise DevError("READ_ONLY", "只读项目不能建立工作流", 403)
            requested = args["assignee_grant_id"]
            if not principal.admin and requested is not None and requested != principal.grant_id:
                raise DevError("ASSIGNEE_FORBIDDEN", "MCP 调用不能为其他授权建立任务", 403)
            assignee = requested if principal.admin else principal.grant_id
            if principal.admin and assignee is not None:
                grant = self.store.one("SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?", (assignee, principal.user_id, principal.space_id))
                if not grant:
                    raise DevError('INVALID_ASSIGNEE', '只能委派给自己在当前空间中的连接', 403)
                try:
                    scopes, allowed, _ = effective_grant(self.store, grant)
                    self.runtime.authorize(self.runtime.grant_principal(grant), 'write', project_id=project['id'])
                except DevError as exc:
                    raise DevError("INVALID_ASSIGNEE", "该授权或访问 Profile 已停用", 403) from exc
                if not {"read", "write"}.issubset(scopes):
                    raise DevError("INVALID_ASSIGNEE", "请选择未撤销且具有读取、写入权限的 MCP 授权", 403)
                if not self.store.one("SELECT 1 AS active FROM tokens WHERE grant_id=? AND expires>? LIMIT 1", (assignee, time.time())):
                    raise DevError("INVALID_ASSIGNEE", "该授权已无有效令牌，请先重新建立可用的 MCP 连接", 403)
                if "*" not in allowed and project["id"] not in allowed:
                    raise DevError("INVALID_ASSIGNEE", "该 MCP 授权不能访问此项目", 403)
            fingerprint = digest(json.dumps({"action": "create", "args": args, "project_id": project["id"]}, sort_keys=True))
            old = self.replay(args, principal, fingerprint)
            if old:
                return old
            source = args["steps"] if args["template"] == "custom" else TEMPLATES[args["template"]]["steps"]
            steps = [{"id": f"s{i+1}", **s, "state": "pending", "summary": "", "evidence": []} for i, s in enumerate(source)]
            now, identifier = time.time(), uuid.uuid4().hex
            self.store.db.execute("INSERT INTO workflows(id,project_id,device_id,root,actor,grant_id,title,goal,template,state,steps,summary,version,created,updated,space_id,owner_user_id) VALUES (?,?,?,?,?,?,?,?,?,'active',?,'',1,?,?,?,?)",
                                  (identifier, project["id"], project["device_id"], project["root"], principal.actor, assignee,
                                   args["title"], args["goal"], args["template"], json.dumps(steps, ensure_ascii=False), now, now, principal.space_id, principal.user_id))
            return self.record({"id": identifier, "version": 1, "state": "active", "updated": now}, args, principal, fingerprint, "create", args["goal"], [])

    def update(self, args, principal):
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.load(args["workflow_id"], principal, write=True)
            fingerprint = digest(json.dumps({"action": "update", "args": args}, sort_keys=True))
            old = self.replay(args, principal, fingerprint)
            if old:
                return old
            if row["version"] != args["expected_version"]:
                raise DevError("VERSION_CONFLICT", "工作流已更新；先 workflows_get 获取当前版本再合并进度", 409, current_version=row["version"])
            if row["state"] in {"completed", "cancelled"}:
                raise DevError("WORKFLOW_CLOSED", "工作流已结束；请为新的工作建立任务", 409)
            action = args["action"]
            evidence = self.evidence(args["evidence"], row, principal)
            if action == "checkpoint":
                if row["state"] != "active":
                    raise DevError("WORKFLOW_BLOCKED", "请先记录解除阻碍的原因并 resume", 409)
                if args["step_id"]:
                    step = next((s for s in row["steps"] if s["id"] == args["step_id"]), None)
                    if step is None:
                        raise DevError("STEP_NOT_FOUND", "步骤不存在", 404)
                    state = args["step_state"]
                    if state == "running" and any(s["state"] == "running" and s["id"] != step["id"] for s in row["steps"]):
                        raise DevError("STEP_ALREADY_RUNNING", "先更新当前步骤，不能同时标记多个当前步骤", 409)
                    if state == "completed":
                        evidence = self.evidence(args["evidence"], row, principal, must_succeed=True)
                    step.update(state=state, summary=args["summary"], evidence=evidence)
            elif action == "block":
                row["state"] = "blocked"
            elif action == "resume":
                if row["state"] != "blocked":
                    raise DevError("WORKFLOW_NOT_BLOCKED", "只有受阻工作流需要 resume", 409)
                row["state"] = "active"
            elif action == "cancel":
                row["state"] = "cancelled"
            elif action == "complete":
                if row["state"] != "active" or not any(s["state"] == "completed" for s in row["steps"]) or any(s["state"] not in {"completed", "skipped"} for s in row["steps"]):
                    raise DevError("INCOMPLETE_STEPS", "必须完成验收或说明跳过原因；不能将全部跳过当作完成", 409)
                for step in row["steps"]:
                    if step["state"] == "completed":
                        step["evidence"] = self.evidence([e["operation_id"] for e in step["evidence"]], row, principal, must_succeed=True)
                row["state"] = "completed"
            row["version"] += 1
            row["updated"] = time.time()
            self.store.db.execute("UPDATE workflows SET state=?,steps=?,summary=?,version=?,updated=? WHERE id=?",
                                  (row["state"], json.dumps(row["steps"], ensure_ascii=False), args["summary"], row["version"], row["updated"], row["id"]))
            return self.record(row, args, principal, fingerprint, action, args["summary"], evidence)
