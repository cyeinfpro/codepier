"""Continuous authority for a particular room input and approved revision."""

from __future__ import annotations

from dataclasses import asdict
import json

from hub.collaboration.common import canonical, digest
from hub.principal import Principal, refresh_principal
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_input_authority (
        message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        source_digest TEXT NOT NULL, evidence TEXT NOT NULL, evidence_digest TEXT NOT NULL)""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS room_input_authority_immutable
        BEFORE UPDATE ON room_input_authority
        BEGIN SELECT RAISE(ABORT,'room input authority is immutable'); END""")
    db.execute("""CREATE TABLE IF NOT EXISTS room_input_resource_revisions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, resource_id TEXT NOT NULL,
        value TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_input_resource_history
        ON room_input_resource_revisions(kind,resource_id,sequence)""")
    # Record changes to authorization ceilings in their own write transactions.
    # Only authorization data needed for continuity is retained.
    for table, kind, identifier, fields in (
        ("projects", "project", "id", ("device_id", "root", "mode", "allow_tasks", "space_id")),
        (
            "grants",
            "grant",
            "id",
            ("scopes", "projects", "revoked", "authorization_mode", "role_id"),
        ),
        ("access_profiles", "profile", "id", ("scopes", "projects", "enabled")),
        ("access_roles", "role", "id", ("policy", "enabled")),
        (
            "vps_connections",
            "target",
            "id",
            ("host", "port", "username", "host_key_policy", "enabled"),
        ),
        ("vps_projects", "target_binding", "vps_id || ':' || {row}.project_id", ("binding_id",)),
    ):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not set(fields) <= columns:
            continue
        for event, row in (("UPDATE", "NEW"), ("DELETE", "OLD")):
            key = f"{row}.{identifier.format(row=row)}"
            value = (
                "json_object(" + ",".join(f"'{field}',{row}.{field}" for field in fields) + ")"
                if event == "UPDATE"
                else "'null'"
            )
            when = (
                " WHEN " + " OR ".join(f"OLD.{f} IS NOT NEW.{f}" for f in fields)
                if event == "UPDATE"
                else ""
            )
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS room_input_history_{kind}_{event.lower()}
                AFTER {event} ON {table}{when} BEGIN
                    INSERT INTO room_input_resource_revisions(kind,resource_id,value) VALUES ('{kind}',{key},{value});
                END""")


def rule_key(rule):
    return rule["project_id"], rule["environment_id"]


def as_rule(value):
    def values(key):
        item = value[key]
        return json.loads(item) if isinstance(item, str) else item

    return {
        "project_id": value["project_id"],
        "environment_id": value["environment_id"],
        "capabilities": values("capabilities"),
        "execution_targets": values("execution_targets"),
        "expires_at": value.get("lifetime", {}).get("expires_at", value.get("expires_at")),
    }


def intersect_grants(now, original, *live_grants):
    if original is None:
        return None
    capabilities = set(original["capabilities"])
    targets = set(original["execution_targets"])
    deadlines = []
    for grant in (original, *live_grants):
        if grant is None:
            return None
        capabilities.intersection_update(grant["capabilities"])
        targets.intersection_update(grant["execution_targets"])
        if grant["expires_at"] is not None:
            deadlines.append(grant["expires_at"])
    expires_at = min(deadlines, default=None)
    if expires_at is not None and now >= expires_at:
        return None
    if not targets:
        capabilities.discard("execute")
    if "read" not in capabilities:
        return None
    return {
        **original,
        "capabilities": sorted(capabilities),
        "execution_targets": sorted(targets) if "execute" in capabilities else [],
        "expires_at": expires_at,
    }


class RoomInputAuthority:
    def __init__(self, bridge):
        self.c, self.store, self.bridge = bridge.c, bridge.store, bridge

    @staticmethod
    def source_key(message):
        return {
            "message_id": message["id"],
            "conversation_id": message["conversation_id"],
            "source_version": message["version"],
            "source_digest": digest(json.loads(message["body"]).get("body_text", "")),
        }

    @staticmethod
    def policy_rules(policy, channel=None):
        room = {rule_key(r): as_rule(r) for r in policy["scope"]["project_scopes"]}
        entry = policy["channels"].get(channel, {}) if channel else {}
        if entry.get("mode") != "custom":
            return room
        return {
            rule_key(r): as_rule(r) for r in entry["scope"]["project_scopes"] if rule_key(r) in room
        }

    def usable_rule(self, rule, actor):
        try:
            project = self.c.runtime.project(rule["project_id"], actor)
        except DevError:
            return None
        capabilities, targets = [], {}
        for capability in rule["capabilities"]:
            if (capability == "write" and project["mode"] != "write") or (
                capability == "execute" and not project["allow_tasks"]
            ):
                continue
            try:
                self.c.coordination.require_capability(actor, capability, project["id"])
                capabilities.append(capability)
            except DevError:
                continue
        if "execute" in capabilities:
            for target in rule["execution_targets"]:
                try:
                    target_snapshot = self.c.delegation.target_snapshot(actor, project, target)
                except DevError:
                    continue
                if target == "project_agent":
                    target_snapshot = {"target": target}
                targets[target] = target_snapshot
        bounded = intersect_grants(
            self.c.clock(),
            rule,
            {**rule, "capabilities": capabilities, "execution_targets": list(targets)},
        )
        if not bounded:
            return None
        bounded["project_identity"] = {k: project[k] for k in ("device_id", "root", "space_id")}
        bounded["target_snapshots"] = targets
        return bounded

    def member_evidence(self, member, owner):
        actor = owner
        if member["grant_id"]:
            try:
                actor = self.c.rooms.principal(member)
            except DevError:
                return {
                    "grant_id": member["grant_id"],
                    "binding_generation": member["binding_generation"],
                    "rules": [],
                }
        rules = []
        for row in self.bridge.scopes(member):
            rule = self.usable_rule(as_rule(row), actor)
            if rule:
                rules.append({**rule, "version": row["version"]})
        return {
            "grant_id": member["grant_id"],
            "binding_generation": member["binding_generation"],
            "profile_id": actor.profile_id,
            "role_id": actor.role_id,
            "rules": rules,
        }

    def ingress_evidence(self, owner, room, member, *, binding=None, channel_scope=None, auth=None):
        permission = self.c.room_permissions.row(room["id"])
        policy = self.c.room_permissions.policy(room["id"])
        room_rules = self.policy_rules(policy)
        if not permission and member:
            # Legacy rooms have member approvals rather than a canonical rule.
            # Every participant still needs their own recorded member approval.
            room_rules = {}
            for current in self.store.all(
                "SELECT * FROM room_members WHERE conversation_id=? AND status NOT IN ('removed','legacy')",
                (room["id"],),
            ):
                for row in self.bridge.scopes(current):
                    key, rule = rule_key(row), as_rule(row)
                    prior = room_rules.get(key)
                    if prior:
                        rule = {
                            **rule,
                            "capabilities": sorted(
                                set(prior["capabilities"]) | set(rule["capabilities"])
                            ),
                            "execution_targets": sorted(
                                set(prior["execution_targets"]) | set(rule["execution_targets"])
                            ),
                            "expires_at": None,
                        }
                    room_rules[key] = rule
        channel = None
        if binding:
            if not auth or not channel_scope:
                return {}
            channel = {
                k: binding[k] for k in ("account_id", "peer_id", "credential_version", "generation")
            }
            channel.update(
                binding_id=binding["id"],
                authorization_id=auth["id"],
                authorization_version=auth["version"],
            )
            scopes = {rule_key(r): as_rule(r) for r in channel_scope["projects"]}
            channel_policy = self.policy_rules(policy, "weixin") if permission else scopes
            room_rules = {
                key: effective
                for key, original in room_rules.items()
                if (
                    effective := intersect_grants(
                        self.c.clock(), original, scopes.get(key), channel_policy.get(key)
                    )
                )
            }
        members = self.store.all(
            "SELECT * FROM room_members WHERE conversation_id=? AND status NOT IN ('removed','legacy')",
            (room["id"],),
        )
        owner_principal = {
            **asdict(owner),
            "scopes": sorted(owner.scopes),
            "session_hash": "",
            "token_hash": "",
        }
        return {
            "schema_version": 1,
            "owner_user_id": owner.user_id,
            "space_id": owner.space_id,
            "owner_principal": owner_principal,
            "policy_version": permission["version"] if permission else None,
            "resource_sequence": self.store.one(
                "SELECT COALESCE(MAX(sequence),0) n FROM room_input_resource_revisions"
            )["n"],
            "room_rules": [
                bounded
                for rule in room_rules.values()
                if (bounded := self.usable_rule(rule, owner))
            ],
            "allow_unassigned": member is None,
            "members": {row["id"]: self.member_evidence(row, owner) for row in members},
            "channel": channel,
        }

    def capture(self, message, owner, member, *, evidence=None):
        key = self.source_key(message)
        if evidence is None:
            room = self.store.one(
                "SELECT * FROM conversation_rooms WHERE id=?", (message["conversation_id"],)
            )
            if not room or room["owner_user_id"] != owner.user_id:
                return
            origin = json.loads(message["body"]).get("external_origin", {})
            binding = auth = channel_scope = None
            if message["origin"] == "external_channel":
                binding = self.store.one(
                    "SELECT * FROM weixin_bindings WHERE id=?", (origin.get("binding_id", ""),)
                )
                record = self.store.one(
                    "SELECT * FROM room_channel_owner_inputs WHERE message_id=?", (message["id"],)
                )
                auth = (
                    self.store.one(
                        "SELECT * FROM room_channel_owner_authorizations WHERE id=?",
                        (record["authorization_id"],),
                    )
                    if record
                    else None
                )
                if not binding or not auth:
                    return
                channel_scope = self.c.room_channels.native.input_scope(message, auth)
            elif message["origin"] != "panel_owner" or message["author"] != owner.user_id:
                return
            evidence = self.ingress_evidence(
                owner, room, member, binding=binding, channel_scope=channel_scope, auth=auth
            )
        if not evidence:
            return
        value = {**evidence, **key, "explicit_continuation": False}
        self.store.execute(
            """INSERT OR IGNORE INTO room_input_authority VALUES (?,?,?,?,?,?)""",
            (*key.values(), canonical(value), digest(value)),
        )

    def load(self, message):
        row = self.store.one(
            "SELECT * FROM room_input_authority WHERE message_id=?", (message["id"],)
        )
        if not row:
            return None
        value = json.loads(row["evidence"])
        if digest(value) != row["evidence_digest"]:
            raise DevError("ROOM_INPUT_APPROVAL_CHANGED", "原消息的入站授权证据不完整", 403)
        return value

    def channel_identity(self, message):
        origin = json.loads(message["body"]).get("external_origin", {})
        binding = self.store.one(
            "SELECT * FROM weixin_bindings WHERE id=?", (origin.get("binding_id", ""),)
        )
        if (
            not origin.get("native_room")
            or origin.get("channel") != "weixin"
            or not binding
            or binding["state"] != "active"
            or not binding["allow_replies"]
            or not binding["peer_id"]
            or binding["conversation_id"] != message["conversation_id"]
            or origin.get("provider_deleted")
            or any(binding[k] != origin.get(k) for k in ("account_id", "peer_id", "generation"))
        ):
            raise DevError("CHANNEL_OWNER_SOURCE_CHANGED", "原微信消息或私聊绑定已失效", 403)
        # Revalidate the original channel grant independently of live account
        # transport. Inbound polling and actual outbound dispatch still require it.
        _, account, owner, room = self.c.room_channels.native._binding_identity(
            binding, allow_unready=True
        )
        latest = self.store.one(
            """SELECT message_id FROM weixin_inbound WHERE account_id=? AND generation=?
            AND peer_id=? AND external_id=?""",
            (
                account["id"],
                binding["generation"],
                binding["peer_id"],
                origin["external_message_id"],
            ),
        )
        if not latest or latest["message_id"] != message["id"] or message.get("reply_to_id"):
            raise DevError(
                "CHANNEL_OWNER_SOURCE_SUPERSEDED", "原微信消息已修订或删除，请核对原始来源", 409
            )
        return binding, owner, room

    def channel_authorization(self, binding):
        auth = self.store.one(
            "SELECT * FROM room_channel_owner_authorizations WHERE binding_id=? AND state='active'",
            (binding["id"],),
        )
        if not auth or digest(json.loads(auth["scope"])) != auth["scope_digest"]:
            raise DevError("CHANNEL_OWNER_NOT_APPROVED", "原微信本人任务批准已撤销或更改", 403)
        return auth

    def continuation_evidence(self, message, owner, member):
        owner, room = self.c.rooms.owner(owner, message["conversation_id"])
        binding = auth = channel_scope = None
        if message["origin"] == "external_channel":
            binding, _, _ = self.channel_identity(message)
            auth = self.channel_authorization(binding)
            channel_scope = self.c.room_channels.native.approved_scope(auth, binding, owner)
            if not channel_scope:
                raise DevError(
                    "CHANNEL_OWNER_SCOPE_CHANGED", "原微信身份或当前授权范围无法核实", 403
                )
        elif message["origin"] != "panel_owner" or message["author"] != owner.user_id:
            raise DevError("ROOM_OWNER_SOURCE_REQUIRED", "只能续办仍可核验的本人任务", 403)
        return {
            **self.ingress_evidence(
                owner, room, member, binding=binding, channel_scope=channel_scope, auth=auth
            ),
            **self.source_key(message),
            "explicit_continuation": True,
        }

    def verified(self, message, proof):
        if not proof:
            body = json.loads(message["body"])
            reason = (
                body.get("owner_task_blocker") if message["origin"] == "external_channel" else None
            )
            raise DevError(
                reason or "ROOM_INPUT_APPROVAL_REQUIRED", "原消息缺少可核验的入站授权记录", 403
            )
        current = self.store.one(
            "SELECT * FROM collaboration_messages WHERE id=?", (message["id"],)
        )
        key = self.source_key(message)
        if (
            not current
            or any(proof.get(k) != v for k, v in key.items())
            or self.source_key(current) != key
            or current["state"] in {"deleted", "withdrawn", "superseded"}
        ):
            raise DevError("ROOM_SOURCE_CHANGED", "原消息已修订、撤回或删除", 409)
        room = self.store.one(
            "SELECT * FROM conversation_rooms WHERE id=?", (message["conversation_id"],)
        )
        if not room or room["state"] != "active" or room["owner_user_id"] != proof["owner_user_id"]:
            raise DevError("ROOM_OWNER_SOURCE_REQUIRED", "原房主或房间已改变", 403)
        owner = refresh_principal(
            self.store,
            Principal(
                **{**proof["owner_principal"], "scopes": set(proof["owner_principal"]["scopes"])}
            ),
        )
        self.c.owner(owner)
        channel = proof.get("channel")
        if channel:
            binding, owner, _ = self.channel_identity(current)
            auth = self.channel_authorization(binding)
            if (
                auth["id"] != channel["authorization_id"]
                or auth["version"] != channel["authorization_version"]
                or owner.user_id != proof["owner_user_id"]
                or owner.space_id != proof["space_id"]
                or binding["id"] != channel["binding_id"]
                or any(
                    binding[k] != channel[k]
                    for k in ("account_id", "peer_id", "credential_version", "generation")
                )
            ):
                raise DevError(
                    "CHANNEL_OWNER_NOT_APPROVED", "原微信本人任务许可或绑定世代已改变", 403
                )
            if not proof.get("explicit_continuation"):
                record = self.store.one(
                    "SELECT * FROM room_channel_owner_inputs WHERE message_id=?", (message["id"],)
                )
                if (
                    not record
                    or record["authorization_id"] != auth["id"]
                    or record["authorization_version"] != auth["version"]
                    or record["message_digest"] != key["source_digest"]
                ):
                    raise DevError(
                        "CHANNEL_OWNER_NOT_APPROVED", "原微信消息缺少完整的本人任务证明", 403
                    )
                self.c.room_channels.native.input_scope(message, auth)
            return {
                "kind": "verified_external_owner",
                "authenticated": True,
                "channel": "weixin",
                "user_id": owner.user_id,
                "authorization_id": auth["id"],
                "authorization_version": auth["version"],
            }
        if current["origin"] != "panel_owner" or current["author"] != proof["owner_user_id"]:
            raise DevError("ROOM_OWNER_SOURCE_REQUIRED", "原房间消息身份无法核验", 403)
        return {"kind": "panel_owner", "authenticated": True, "user_id": current["author"]}

    def policy_history(self, proof):
        current = self.c.room_permissions.row(proof["conversation_id"])
        start = proof["policy_version"]
        if not current:
            return [] if start is None else [({}, {})]
        floor = -1 if start is None else start
        rows = self.store.all(
            """SELECT version,policy FROM room_permission_revisions
            WHERE conversation_id=? AND version>? ORDER BY version""",
            (proof["conversation_id"], floor),
        )
        expected = current["version"] - floor
        if start is None and rows and rows[0]["version"] == 1:
            expected -= 1  # Legacy rooms have no version-zero approval.
        if len(rows) != expected or (rows and rows[-1]["policy"] != current["policy"]):
            return [({}, {})]
        result = []
        for row in [*rows, current]:
            policy = json.loads(row["policy"])
            result.append(
                (
                    self.policy_rules(policy),
                    self.policy_rules(policy, "weixin") if proof["channel"] else None,
                )
            )
        return result

    def member_history(self, member, saved, rule, current):
        key = rule_key(rule)
        if not current:
            return [None]
        if saved is None:
            return [as_rule(current)]
        revisions = self.store.all(
            """SELECT version,snapshot FROM room_member_scope_revisions
            WHERE member_id=? AND project_id=? AND environment_id=? AND version>? ORDER BY version""",
            (member["id"], *key, saved["version"]),
        )
        if len(revisions) != current["version"] - saved["version"]:
            return [None]
        return [
            as_rule(row) if row["state"] == "active" else None
            for row in [*(json.loads(r["snapshot"]) for r in revisions), current]
        ]

    def resources(self, proof, kind, identifier):
        return (
            json.loads(row["value"])
            for row in self.store.all(
                """SELECT value FROM room_input_resource_revisions
            WHERE kind=? AND resource_id=? AND sequence>? ORDER BY sequence""",
                (kind, identifier, proof["resource_sequence"]),
            )
        )

    def continuous_resources(self, proof, rule, saved, live):
        project = rule["project_id"]
        capabilities = set(rule["capabilities"])
        targets = set(rule["execution_targets"])
        for state in self.resources(proof, "project", project):
            if not state or any(
                state[k] != rule["project_identity"][k] for k in ("device_id", "root", "space_id")
            ):
                return None
            if state["mode"] != "write":
                capabilities.discard("write")
            if not state["allow_tasks"]:
                capabilities.discard("execute")
        if saved:
            for kind, key in (("grant", "grant_id"), ("profile", "profile_id")):
                if not saved.get(key):
                    continue
                for state in self.resources(proof, kind, saved[key]):
                    if not state or state.get("revoked") or not state.get("enabled", True):
                        return None
                    if kind == "grant" and state["authorization_mode"] == "role":
                        if state["role_id"] != saved.get("role_id"):
                            return None
                        continue
                    projects = json.loads(state["projects"])
                    if project not in projects and "*" not in projects:
                        return None
                    capabilities.intersection_update(json.loads(state["scopes"]))
            if role_id := saved.get("role_id"):
                from hub.roles import RolePolicy, created_ids, project_actions

                created = created_ids(self.store, role_id)
                for state in self.resources(proof, "role", role_id):
                    if not state or not state["enabled"]:
                        return None
                    capabilities.intersection_update(
                        project_actions(
                            RolePolicy.model_validate_json(state["policy"]), project, created
                        )
                    )
        if live and live["project_identity"] != rule["project_identity"]:
            return None
        for target in tuple(targets):
            original = rule["target_snapshots"].get(target)
            if live and live["target_snapshots"].get(target) != original:
                targets.remove(target)
                continue
            if target.startswith("vps:"):
                target_id = target[4:]
                if any(
                    not state
                    or not state["enabled"]
                    or any(state[k] != original["endpoint"][k] for k in original["endpoint"])
                    for state in self.resources(proof, "target", target_id)
                ) or any(
                    True for _ in self.resources(proof, "target_binding", target_id + ":" + project)
                ):
                    targets.remove(target)
        return {**rule, "capabilities": capabilities, "execution_targets": targets}

    def proof(self, message, member=None, goal=None):
        from hub.collaboration.room_continuations import RoomContinuations

        continuations = RoomContinuations(self.c)
        value = (
            continuations.work_proof(goal, message)
            if goal
            else continuations.source_proof(message, member)
        )
        return value or self.load(message)

    def rules(self, message, member=None, *, goal=None, proof=None, recovered=False, keys=None):
        if proof is None:
            proof = self.proof(message, member, goal)
        self.verified(message, proof)
        policy_history = self.policy_history(proof)
        saved = proof["members"].get(member["id"]) if member else None
        body = json.loads(message["body"])
        if (
            member
            and not saved
            and not recovered
            and not (proof["allow_unassigned"] and body.get("room_member_id") == member["id"])
        ):
            return {}
        if (
            saved
            and saved["grant_id"]
            and (
                saved["grant_id"] != member["grant_id"]
                or saved["binding_generation"] != member["binding_generation"]
            )
        ):
            if not recovered:
                return {}
            saved = None
        owner = refresh_principal(
            self.store,
            Principal(
                **{**proof["owner_principal"], "scopes": set(proof["owner_principal"]["scopes"])}
            ),
        )
        actor = self.c.rooms.principal(member) if member and member["grant_id"] else None
        saved_rules = {rule_key(rule): rule for rule in saved["rules"]} if saved else {}
        member_scopes = {rule_key(row): row for row in self.bridge.scopes(member)} if member else {}
        result = {}
        for rule in proof["room_rules"]:
            key = rule_key(rule)
            if keys is not None and key not in keys:
                continue
            history = [
                rules.get(key) for pair in policy_history for rules in pair if rules is not None
            ]
            history.append(self.usable_rule(rule, owner))
            current = None
            if member:
                saved_rule = saved_rules.get(key)
                if saved and not saved_rule:
                    continue
                if saved_rule:
                    history.append(saved_rule)
                history.extend(
                    self.member_history(member, saved_rule, rule, member_scopes.get(key))
                )
                current = self.usable_rule(rule, actor) if actor else None
                if actor and not current:
                    continue
                if current:
                    history.append(current)
            history.append(self.continuous_resources(proof, rule, saved, current))
            if effective := intersect_grants(self.c.clock(), rule, *history):
                result[key] = effective
        return result

    def author(self, message, *, project_ids=None, goal=None):
        identifier = json.loads(message["body"]).get("room_member_id")
        member = (
            self.store.one("SELECT * FROM room_members WHERE id=?", (identifier,))
            if identifier
            else None
        )
        proof = self.proof(message, member, goal)
        author = self.verified(message, proof)
        rules = self.rules(message, None if goal else member, goal=goal, proof=proof)
        if not rules or (
            project_ids is not None and not set(project_ids) <= {key[0] for key in rules}
        ):
            raise DevError(
                "CHANNEL_OWNER_SCOPE_DENIED" if proof["channel"] else "ROOM_WORK_SCOPE_DENIED",
                "原消息没有仍连续有效的所选项目授权",
                403,
            )
        return author
