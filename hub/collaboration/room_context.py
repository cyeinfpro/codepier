"""Versioned, sourced room context over the original message and work ledgers.

This is a read projection and a fenced summary journal, never work authority.
Facts are immutable. Replacements remove the old current pointer atomically;
historical source evidence is retained without returning contradictory current
facts. Every read rechecks current member and original message visibility.
"""

from __future__ import annotations

import json
import uuid

from hub.collaboration.projection_cache import projection, memoized
from hub.principal import refresh_principal
from hub.collaboration.common import canonical, digest, read_cursor, redact, sign_cursor, validate
from shared.room_context_contracts import RoomContextRead, RoomContextUpdate
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_context_state(
        conversation_id TEXT PRIMARY KEY REFERENCES conversation_rooms(id),
        version INTEGER NOT NULL,updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS room_context_facts(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),
        kind TEXT NOT NULL,fact_key TEXT NOT NULL,text TEXT NOT NULL,sources TEXT NOT NULL,
        project_ids TEXT NOT NULL,artifact_refs TEXT NOT NULL,author_member_id TEXT NOT NULL,
        binding_generation INTEGER NOT NULL,grant_id TEXT NOT NULL,context_version INTEGER NOT NULL,
        created REAL NOT NULL)""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS room_context_facts_immutable
        BEFORE UPDATE ON room_context_facts BEGIN SELECT RAISE(ABORT,'room context evidence is immutable'); END""")
    for name, definition in (
        ("scope_refs", "TEXT NOT NULL DEFAULT '[]'"),
        ("source_floor_sequence", "INTEGER NOT NULL DEFAULT 0"),
        ("source_floor_owner", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in {row[1] for row in db.execute("PRAGMA table_info(room_context_facts)")}:
            db.execute("ALTER TABLE room_context_facts ADD COLUMN " + name + " " + definition)
    db.execute("""CREATE TABLE IF NOT EXISTS room_context_current(
        conversation_id TEXT NOT NULL,kind TEXT NOT NULL,fact_key TEXT NOT NULL,
        fact_id TEXT NOT NULL UNIQUE REFERENCES room_context_facts(id),
        PRIMARY KEY(conversation_id,kind,fact_key))""")
    db.execute("""CREATE TABLE IF NOT EXISTS room_context_supersessions(
        fact_id TEXT PRIMARY KEY REFERENCES room_context_facts(id),message_id TEXT NOT NULL,
        message_version INTEGER NOT NULL,context_version INTEGER NOT NULL,replacement_fact_id TEXT,
        created REAL NOT NULL)""")
    for name, definition in (
        ("message_sequence", "INTEGER NOT NULL DEFAULT 0"),
        ("message_digest", "TEXT NOT NULL DEFAULT ''"),
        ("requires_owner_source", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in {
            row[1] for row in db.execute("PRAGMA table_info(room_context_supersessions)")
        }:
            db.execute(
                "ALTER TABLE room_context_supersessions ADD COLUMN " + name + " " + definition
            )
    db.execute("""CREATE TABLE IF NOT EXISTS room_context_reviewed_sources(
        conversation_id TEXT NOT NULL,message_id TEXT NOT NULL,version INTEGER NOT NULL,digest TEXT NOT NULL,
        context_version INTEGER NOT NULL,PRIMARY KEY(conversation_id,message_id))""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_context_facts_page
        ON room_context_facts(conversation_id,sequence)""")


class RoomContext:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def version(self, conversation_id):
        row = self.store.one(
            "SELECT version FROM room_context_state WHERE conversation_id=?", (conversation_id,)
        )
        return row["version"] if row else 0

    def check_version(self, conversation_id, expected):
        current = self.version(conversation_id)
        if type(expected) is not int or expected != current:
            raise DevError(
                "CONTEXT_VERSION_CHANGED",
                "房间上下文已更新，请核对当前事实后重试",
                409,
                context_version=current,
            )
        return current

    @memoized
    def projects(self, principal, room, member=None):
        if member:
            readable = self.c.rooms.read_scope_keys(member, principal)
            return [
                {
                    "project_id": r["project_id"],
                    "environment_id": r["environment_id"],
                    "capabilities": sorted({"read", *r["capabilities"]}),
                    "execution_target_count": len(r.get("execution_targets", [])),
                }
                for r in self.c.rooms.scopes(member, principal)
                if (r["project_id"], r["environment_id"]) in readable
            ]
        return [
            {
                "project_id": r["project_id"],
                "environment_id": r["environment_id"],
                "project_label": r["project_label"],
            }
            for r in self.c.conversations.visible(
                principal, room, include_archived=room["state"] == "archived"
            )
        ]

    def message_scopes(self, row):
        result = {
            (s["project_id"], s["environment_id"])
            for s in json.loads(row["body"]).get("provenance_scopes", [])
        }
        if row.get("room_id"):
            partition = self.store.one(
                "SELECT project_id,environment_id FROM collaboration_rooms WHERE id=?",
                (row["room_id"],),
            )
            if partition:
                result.add((partition["project_id"], partition["environment_id"]))
        return result

    @staticmethod
    def scope_refs(scopes):
        return [{"project_id": p, "environment_id": e} for p, e in sorted(scopes)]

    def fact_scopes(self, row):
        result = {
            (s["project_id"], s["environment_id"]) for s in json.loads(row.get("scope_refs", "[]"))
        }
        for ref in json.loads(row["sources"]):
            source = self.store.one(
                "SELECT * FROM collaboration_messages WHERE id=?", (ref["message_id"],)
            )
            if source:
                result |= self.message_scopes(source)
        return result

    def message_projects(self, row):
        projects = set(json.loads(row["body"]).get("provenance_project_ids", []))
        if row.get("room_id"):
            partition = self.store.one(
                "SELECT project_id FROM collaboration_rooms WHERE id=?", (row["room_id"],)
            )
            if partition:
                projects.add(partition["project_id"])
        return projects

    @memoized
    def source(self, reference, principal, room, member=None):
        row = self.store.one(
            "SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?",
            (reference["message_id"], room["id"]),
        )
        if not self.source_visible(row, principal, room, member):
            raise DevError("CONTEXT_SOURCE_NOT_FOUND", "当前房间授权没有这条上下文来源", 404)
        body = json.loads(row["body"])
        if (
            row["version"] != reference["version"]
            or digest(body.get("body_text", "")) != reference["digest"]
            or body.get("external_origin", {}).get("provider_deleted")
        ):
            raise DevError(
                "CONTEXT_SOURCE_CHANGED", "原消息版本或正文已变化，需重新核对上下文", 409
            )
        if body.get("external_origin", {}).get("native_room"):
            origin = body["external_origin"]
            latest = self.store.one(
                "SELECT message_id FROM weixin_inbound WHERE account_id=? AND generation=? AND peer_id=? AND external_id=?",
                (
                    origin.get("account_id", ""),
                    origin.get("generation"),
                    origin.get("peer_id", ""),
                    origin.get("external_message_id", ""),
                ),
            )
            if latest and latest["message_id"] != row["id"]:
                raise DevError("CONTEXT_SOURCE_CHANGED", "原渠道消息已有更新版本", 409)
        return row

    @memoized
    def source_visible(self, row, principal, room, member=None):
        if not row or row["conversation_id"] != room["id"]:
            return False
        if row.get("room_id") and not self.store.one(
            """SELECT 1 FROM conversation_projects
                WHERE conversation_id=? AND room_id=?""",
            (room["id"], row["room_id"]),
        ):
            if (
                member
                or room["state"] != "archived"
                or principal.grant_id
                or row["room_id"]
                not in {
                    p["id"]
                    for p in self.c.conversations.visible(principal, room, include_archived=True)
                }
            ):
                return False
        allowed = {
            (p["project_id"], p["environment_id"]) for p in self.projects(principal, room, member)
        }
        if not self.message_scopes(row) <= allowed:
            return False
        return bool(
            self.c.chatroom.message_visible(principal, row)
            and (not member or self.c.room_consumers._visible_message(member, principal, row))
        )

    def owner_source(self, row, room):
        if row["origin"] == "panel_owner" and row["author"] == room["owner_user_id"]:
            return True
        origin = json.loads(row["body"]).get("external_origin", {})
        if row["origin"] == "external_channel" and origin.get("native_room"):
            binding = self.store.one(
                "SELECT * FROM weixin_bindings WHERE id=?", (origin.get("binding_id", ""),)
            )
            if not (
                binding
                and binding["conversation_id"] == room["id"]
                and binding["owner_user_id"] == room["owner_user_id"]
                and binding["space_id"] == room["space_id"]
                and binding["account_id"] == origin.get("account_id")
                and binding["peer_id"] == origin.get("peer_id")
                and binding["generation"] == origin.get("generation")
            ):
                return False
            # Ownership comes from the paired inbound ledger, never display
            # fields on an arbitrary message. Task consent remains separate.
            evidence = self.store.one(
                """SELECT message_id FROM weixin_inbound_versions WHERE account_id=?
                AND generation=? AND peer_id=? AND external_id=? AND provider_version=?""",
                (
                    binding["account_id"],
                    binding["generation"],
                    binding["peer_id"],
                    origin.get("external_message_id", ""),
                    origin.get("provider_version", ""),
                ),
            )
            if not evidence or evidence["message_id"] != row["id"]:
                return False
            expected_author = (
                "weixin:" + binding["account_id"] + ":" + digest(binding["peer_id"])[:20]
            )
            if row["author"] != expected_author:
                return False
            try:
                if room["state"] == "archived":
                    from hub.principal import Principal

                    owner = refresh_principal(
                        self.store,
                        Principal(**{**json.loads(binding["owner_principal"]), "scopes": set()}),
                    )
                    self.c.rooms.owner(owner, room["id"], include_archived=True)
                    account = self.c.weixin.account(binding["account_id"], owner)
                    if (
                        account["generation"] != binding["generation"]
                        or account["credential_version"] != binding["credential_version"]
                    ):
                        return False
                else:
                    self.c.room_channels.native._valid(binding, require_member=False)
            except DevError:
                return False
            return True
        return False

    def artifacts(self, refs, sources, principal, room, member=None):
        scopes = set()
        allowed = {
            (row["project_id"], row["environment_id"])
            for row in self.projects(principal, room, member)
        }
        native = self.c.room_work_bridge.native
        for ref in refs:
            item = self.store.one(
                "SELECT * FROM coordination_work WHERE id=?", (ref["work_item_id"],)
            )
            goal = (
                self.store.one("SELECT * FROM coordination_goals WHERE id=?", (item["goal_id"],))
                if item
                else None
            )
            if (
                not item
                or not goal
                or goal["conversation_id"] != room["id"]
                or item["state"] != "succeeded"
                or not item["result"]
                or item["version"] != ref["version"]
                or digest(json.loads(item["result"])) != ref["digest"]
            ):
                raise DevError(
                    "CONTEXT_ARTIFACT_CHANGED", "上下文产物必须引用原已完成工作的精确结果版本", 409
                )
            if not any(
                row.get("goal_id") == goal["id"]
                and (
                    json.loads(row["body"]).get("work_item_id") == item["id"]
                    or ref in json.loads(row["body"]).get("artifact_refs", [])
                )
                for row in sources
            ):
                raise DevError(
                    "CONTEXT_ARTIFACT_SOURCE_REQUIRED",
                    "产物引用需要包含原工作结果或最终发表的来源消息",
                    409,
                )
            scope = native.work_scope_key(goal, item)
            if scope not in allowed or (
                native.reference(goal)
                and member
                and scope not in native.visible_scope_keys(goal, principal, member["id"])
            ):
                raise DevError(
                    "CONTEXT_ARTIFACT_NOT_VISIBLE",
                    "当前成员没有此产物所属项目和环境的读取权限",
                    403,
                )
            scopes.add(scope)
        return scopes

    def fact(self, row, principal, room, member=None):
        projects = self.projects(principal, room, member)
        allowed = {p["project_id"] for p in projects}
        if not set(json.loads(row["project_ids"])) <= allowed or not self.fact_scopes(row) <= {
            (p["project_id"], p["environment_id"]) for p in projects
        }:
            raise DevError("CONTEXT_FACT_NOT_FOUND", "当前范围没有这条事实", 404)
        refs = json.loads(row["sources"])
        sources = [self.source(ref, principal, room, member) for ref in refs]
        self.correction_barrier(
            room,
            row["kind"],
            row["fact_key"],
            sources,
            floor_sequence=row["source_floor_sequence"],
            requires_owner_source=bool(row["source_floor_owner"]),
        )
        if row["kind"] in {"objective", "decision", "constraint"} and not any(
            self.owner_source(s, room) for s in sources
        ):
            raise DevError(
                "CONTEXT_SOURCE_CHANGED", "原本人来源身份已变更，请重新核对当前事实", 409
            )
        artifacts = json.loads(row["artifact_refs"])
        self.artifacts(artifacts, sources, principal, room, member)
        return {
            "id": row["id"],
            "kind": row["kind"],
            "key": row["fact_key"],
            "text": row["text"],
            "sources": [
                {**ref, "origin": source["origin"], "read_action": "room_message_read"}
                for ref, source in zip(refs, sources, strict=True)
            ],
            "project_ids": json.loads(row["project_ids"]),
            "artifact_refs": artifacts,
            "project_scopes": self.scope_refs(self.fact_scopes(row)),
            "context_version": row["context_version"],
            "author_member_id": row["author_member_id"],
            "status": "current",
            "grants_authority": False,
        }

    def retirable(self, row, principal, room, member=None):
        """Retirement checks present visibility, not stale evidence accuracy.

        Changed/deleted source versions must not trap an invalid fact forever.
        The immutable old evidence stays in the journal; only its current
        pointer is removed, with a newly validated correction as provenance.
        """
        visible_projects = self.projects(principal, room, member)
        allowed = {p["project_id"] for p in visible_projects}
        if not self.fact_scopes(row) <= {
            (p["project_id"], p["environment_id"]) for p in visible_projects
        }:
            raise DevError("CONTEXT_FACT_NOT_FOUND", "当前范围没有这条事实", 404)
        projects = set(json.loads(row["project_ids"]))
        for ref in json.loads(row["sources"]):
            source = self.store.one(
                "SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?",
                (ref["message_id"], room["id"]),
            )
            if not source:
                if member:
                    raise DevError(
                        "CONTEXT_SOURCE_NOT_FOUND", "原来源不存在，请本人核对后撤回旧事实", 404
                    )
                continue
            if not self.source_visible(source, principal, room, member):
                raise DevError("CONTEXT_FACT_NOT_FOUND", "当前范围没有这条事实", 404)
            projects |= self.message_projects(source)
        if not projects <= allowed:
            raise DevError("CONTEXT_FACT_NOT_FOUND", "当前范围没有这条事实", 404)
        return projects

    def bump(self, conversation_id, version):
        self.store.execute(
            """INSERT INTO room_context_state VALUES (?,?,?) ON CONFLICT(conversation_id)
            DO UPDATE SET version=excluded.version,updated=excluded.updated""",
            (conversation_id, version, self.c.clock()),
        )
        self.c.rooms.change(conversation_id, "context", conversation_id, version)

    def key_floor(self, room, kind, key):
        row = self.store.one(
            """SELECT s.message_sequence,s.requires_owner_source,m.conversation_sequence
            FROM room_context_supersessions s JOIN room_context_facts f ON f.id=s.fact_id
            LEFT JOIN collaboration_messages m ON m.id=s.message_id
            WHERE f.conversation_id=? AND f.kind=? AND f.fact_key=? ORDER BY s.context_version DESC LIMIT 1""",
            (room["id"], kind, key),
        )
        return (
            (
                row["message_sequence"] or row["conversation_sequence"] or 0,
                bool(row["requires_owner_source"]),
            )
            if row
            else (0, False)
        )

    def correction_barrier(
        self, room, kind, key, sources, *, floor_sequence=0, requires_owner_source=False
    ):
        """A fresh lease/CAS never resurrects a fact from pre-correction input."""
        barrier = self.store.one(
            """SELECT s.* FROM room_context_supersessions s
            JOIN room_context_facts f ON f.id=s.fact_id WHERE f.conversation_id=? AND f.kind=? AND f.fact_key=?
            ORDER BY s.context_version DESC LIMIT 1""",
            (room["id"], kind, key),
        )
        if not barrier and not floor_sequence:
            return
        correction = (
            self.store.one(
                "SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?",
                (barrier["message_id"], room["id"]),
            )
            if barrier
            else None
        )
        if barrier and not correction:
            raise DevError(
                "CONTEXT_CORRECTION_SOURCE_REQUIRED",
                "原纠正来源不可用，请本人重新核对当前事实",
                409,
            )
        if barrier:
            floor_sequence = max(
                floor_sequence, barrier["message_sequence"] or correction["conversation_sequence"]
            )
            requires_owner_source = (
                requires_owner_source
                or bool(barrier["requires_owner_source"])
                or self.owner_source(correction, room)
            )

        def current_enough(source):
            if source["conversation_sequence"] < floor_sequence:
                return False
            if (
                correction
                and source["id"] == correction["id"]
                and source["version"] < barrier["message_version"]
            ):
                return False
            return not requires_owner_source or self.owner_source(source, room)

        if not any(current_enough(source) for source in sources):
            raise DevError(
                "CONTEXT_CORRECTION_SOURCE_REQUIRED",
                "此事实已被纠正，重建须引用该纠正或更新的本人原消息",
                409,
            )

    def replace(self, room, rows, source, version, replacements):
        for old in rows:
            self.correction_barrier(
                room,
                old["kind"],
                old["fact_key"],
                [source],
                floor_sequence=old["source_floor_sequence"],
                requires_owner_source=bool(old["source_floor_owner"]),
            )
            prior = self.store.one(
                """SELECT MAX(s.requires_owner_source) n FROM room_context_supersessions s
                JOIN room_context_facts f ON f.id=s.fact_id WHERE f.conversation_id=? AND f.kind=? AND f.fact_key=?""",
                (room["id"], old["kind"], old["fact_key"]),
            )
            requires_owner = int(
                self.owner_source(source, room)
                or bool(prior and prior["n"])
                or old["source_floor_owner"]
            )
            self.store.execute("DELETE FROM room_context_current WHERE fact_id=?", (old["id"],))
            self.store.execute(
                """INSERT INTO room_context_supersessions
                (fact_id,message_id,message_version,context_version,replacement_fact_id,created,
                 message_sequence,message_digest,requires_owner_source) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    old["id"],
                    source["id"],
                    source["version"],
                    version,
                    replacements.get((old["kind"], old["fact_key"])),
                    self.c.clock(),
                    source["conversation_sequence"],
                    self.reference(source)["digest"],
                    requires_owner,
                ),
            )

    def update(self, raw, principal):
        args = validate(RoomContextUpdate, raw)
        with self.store.transaction():
            principal = refresh_principal(self.store, principal)
            member = self.c.room_consumers.authorize_member(
                args, principal, action="context_update"
            )
            room = self.c.conversations.object(principal, member["conversation_id"])
            if room["state"] != "active" or room["lead_member_id"] != member["id"]:
                raise DevError("CONTEXT_LEAD_REQUIRED", "只有当前房间主责可更新上下文", 403)
            allowed_keys = {
                (p["project_id"], p["environment_id"])
                for p in self.projects(principal, room, member)
            }
            allowed = {project for project, _ in allowed_keys}

            def save():
                version = self.check_version(room["id"], args["expected_context_version"]) + 1
                old = []
                old_projects = {}
                old_scopes = {}
                for identifier in args["supersedes"]:
                    row = self.store.one(
                        """SELECT f.* FROM room_context_facts f JOIN room_context_current c
                        ON c.fact_id=f.id WHERE f.id=? AND f.conversation_id=?""",
                        (identifier, room["id"]),
                    )
                    if not row:
                        raise DevError("CONTEXT_FACT_NOT_FOUND", "待替代事实已不是当前事实", 409)
                    old_projects[(row["kind"], row["fact_key"])] = self.retirable(
                        row, principal, room, member
                    )
                    old.append(row)
                    old_scopes[(row["kind"], row["fact_key"])] = self.fact_scopes(row)
                old_ids = {r["id"] for r in old}
                prepared = []
                reviewed = {}
                for fact in args["facts"]:
                    sources = [self.source(ref, principal, room, member) for ref in fact["sources"]]
                    self.correction_barrier(room, fact["kind"], fact["key"], sources)
                    if fact["kind"] in {"objective", "decision", "constraint"} and not any(
                        self.owner_source(s, room) for s in sources
                    ):
                        raise DevError(
                            "CONTEXT_OWNER_SOURCE_REQUIRED",
                            "目标、决定与约束必须指向本人的原消息，助手提议不能视为本人决定",
                            403,
                        )
                    projects = set(fact["project_ids"]) | set().union(
                        *(self.message_projects(s) for s in sources)
                    )
                    # Renaming/merging a retired fact cannot erase its original
                    # project boundary from the replacement's read projection.
                    projects |= old_projects.get(
                        (fact["kind"], fact["key"]), set().union(*old_projects.values())
                    )
                    scopes = set().union(*(self.message_scopes(s) for s in sources))
                    scopes |= old_scopes.get(
                        (fact["kind"], fact["key"]), set().union(*old_scopes.values())
                    )
                    scopes |= self.artifacts(
                        fact["artifact_refs"], sources, principal, room, member
                    )
                    projects |= {project for project, _ in scopes}
                    unspecified = projects - {project for project, _ in scopes}
                    scopes |= {key for key in allowed_keys if key[0] in unspecified}
                    if not projects <= allowed or not scopes <= allowed_keys:
                        raise DevError(
                            "CONTEXT_SCOPE_DENIED",
                            "上下文不能引用当前成员未获批准的项目和环境",
                            403,
                        )
                    current = self.store.one(
                        "SELECT fact_id FROM room_context_current WHERE conversation_id=? AND kind=? AND fact_key=?",
                        (room["id"], fact["kind"], fact["key"]),
                    )
                    if current and current["fact_id"] not in old_ids:
                        raise DevError(
                            "CONTEXT_REPLACEMENT_REQUIRED",
                            "同一当前事实已有内容，请明确替代原事实",
                            409,
                        )
                    identifier = "ctx_" + uuid.uuid4().hex
                    prepared.append(
                        (identifier, fact, sources, sorted(projects), self.scope_refs(scopes))
                    )
                    reviewed.update(
                        {
                            ref["message_id"]: (ref, source)
                            for ref, source in zip(fact["sources"], sources, strict=True)
                        }
                    )
                for ref in args["reviewed_sources"]:
                    reviewed[ref["message_id"]] = (ref, self.source(ref, principal, room, member))
                if old and not reviewed:
                    raise DevError(
                        "CONTEXT_CORRECTION_SOURCE_REQUIRED", "替代原事实必须保留纠正来源", 422
                    )
                count = self.store.one(
                    "SELECT COUNT(*) n FROM room_context_current WHERE conversation_id=?",
                    (room["id"],),
                )["n"]
                if count - len(old) + len(prepared) > 128:
                    raise DevError(
                        "CONTEXT_FACT_LIMIT",
                        "当前上下文最多保留128条事实，请合并或替代已有事实",
                        409,
                    )
                replacements = {
                    (f["kind"], f["key"]): identifier for identifier, f, _, _, _ in prepared
                }
                if old:
                    candidates = [source for _, source in reviewed.values()]
                    owner_sources = [
                        source for source in candidates if self.owner_source(source, room)
                    ]
                    correction = max(
                        owner_sources or candidates, key=lambda s: s["conversation_sequence"]
                    )
                    self.replace(room, old, correction, version, replacements)
                    # The current request may itself introduce a newer owner
                    # correction. Validate its newly persisted barriers before
                    # inserting any replacement, so old evidence cannot ride
                    # along with a fresh reviewed_sources entry in one CAS.
                    for _, fact, sources, _, _ in prepared:
                        original_keys = (
                            [(fact["kind"], fact["key"])]
                            if (fact["kind"], fact["key"]) in old_projects
                            else list(old_projects)
                        )
                        for kind, fact_key in original_keys:
                            self.correction_barrier(room, kind, fact_key, sources)
                for identifier, fact, _, projects, scopes in prepared:
                    ancestors = (
                        [
                            row
                            for row in old
                            if (row["kind"], row["fact_key"]) == (fact["kind"], fact["key"])
                        ]
                        if (fact["kind"], fact["key"]) in old_projects
                        else old
                    )
                    floor_values = [self.key_floor(room, fact["kind"], fact["key"])]
                    for ancestor in ancestors:
                        floor_values.extend(
                            [
                                self.key_floor(room, ancestor["kind"], ancestor["fact_key"]),
                                (
                                    ancestor["source_floor_sequence"],
                                    bool(ancestor["source_floor_owner"]),
                                ),
                            ]
                        )
                    floor_sequence = max(value[0] for value in floor_values)
                    floor_owner = any(value[1] for value in floor_values)
                    self.store.execute(
                        """INSERT INTO room_context_facts
                        (id,conversation_id,kind,fact_key,text,sources,project_ids,artifact_refs,author_member_id,
                         binding_generation,grant_id,context_version,created,scope_refs,source_floor_sequence,source_floor_owner)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            identifier,
                            room["id"],
                            fact["kind"],
                            fact["key"],
                            redact(fact["text"]),
                            canonical(fact["sources"]),
                            canonical(projects),
                            canonical(fact["artifact_refs"]),
                            member["id"],
                            member["binding_generation"],
                            principal.grant_id,
                            version,
                            self.c.clock(),
                            canonical(scopes),
                            floor_sequence,
                            int(floor_owner),
                        ),
                    )
                    self.store.execute(
                        "INSERT INTO room_context_current VALUES (?,?,?,?)",
                        (room["id"], fact["kind"], fact["key"], identifier),
                    )
                for ref, _ in reviewed.values():
                    self.store.execute(
                        """INSERT INTO room_context_reviewed_sources VALUES (?,?,?,?,?) ON CONFLICT(conversation_id,message_id)
                        DO UPDATE SET version=excluded.version,digest=excluded.digest,context_version=excluded.context_version""",
                        (room["id"], ref["message_id"], ref["version"], ref["digest"], version),
                    )
                self.bump(room["id"], version)
                return {
                    "conversation_id": room["id"],
                    "version": version,
                    "fact_ids": [p[0] for p in prepared],
                    "superseded_fact_ids": args["supersedes"],
                    "scheduled": False,
                    "grants_authority": False,
                }

            return self.c.room_runtime.member_receipt(
                args, member, principal, "room_context_update", save
            )

    def correct_owner(self, message, fact_ids, expected_version, principal):
        """Called by the owner's canonical message transaction before enqueue."""
        self.store.require_transaction()
        principal, room = self.c.rooms.owner(principal, message["conversation_id"])
        if room["state"] != "active":
            raise DevError("ROOM_INACTIVE", "房间已暂停，不能纠正当前上下文", 409)
        if not fact_ids:
            return
        if message["origin"] != "panel_owner" or message["author"] != principal.user_id:
            raise DevError("CONTEXT_OWNER_SOURCE_REQUIRED", "明确纠正必须来自本人的新消息", 403)
        version = self.check_version(room["id"], expected_version) + 1
        rows = []
        projects = self.message_projects(message)
        scopes = self.message_scopes(message)
        for identifier in fact_ids:
            row = self.store.one(
                """SELECT f.* FROM room_context_facts f JOIN room_context_current c ON c.fact_id=f.id
                WHERE f.id=? AND f.conversation_id=?""",
                (identifier, room["id"]),
            )
            if not row:
                raise DevError("CONTEXT_FACT_NOT_FOUND", "待纠正事实已不是当前事实", 409)
            projects |= self.retirable(row, principal, room)
            rows.append(row)
            scopes |= self.fact_scopes(row)
        # The owner may quote a scoped fact in their correction. Its new message
        # must retain that provenance before routing or any room projection.
        body = json.loads(message["body"])
        body["provenance_project_ids"] = sorted(projects)
        body["provenance_scopes"] = self.scope_refs(scopes)
        self.store.execute(
            "UPDATE collaboration_messages SET body=? WHERE id=?", (canonical(body), message["id"])
        )
        message = {**message, "body": canonical(body)}
        self.replace(room, rows, message, version, {})
        self.bump(room["id"], version)
        return message

    def reference(self, row):
        return {
            "message_id": row["id"],
            "version": row["version"],
            "digest": digest(json.loads(row["body"]).get("body_text", "")),
        }

    def read(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, actor, room, member, _, _, _ = self.c.room_consumers.read_scope(
                raw, principal, RoomContextRead
            )
            return self.project(
                actor,
                room,
                member,
                cursor=args["cursor"],
                limit=args["limit"],
                byte_budget=args["byte_budget"],
                input_message_id=args["input_message_id"],
            )

    def owner_read(self, conversation_id, principal, *, cursor="", limit=20, byte_budget=16000):
        with self.store.transaction(immediate=False):
            principal, room = self.c.rooms.owner(principal, conversation_id, include_archived=True)
            return self.project(
                principal, room, cursor=cursor, limit=limit, byte_budget=byte_budget
            )

    @projection
    def project(
        self,
        principal,
        room,
        member=None,
        *,
        cursor="",
        limit=20,
        byte_budget=16000,
        input_message_id="",
    ):
        if not 1 <= limit <= 50 or not 4096 <= byte_budget <= 24000:
            raise DevError("INVALID_CONTEXT_PAGE", "上下文分页或字节预算无效", 422)
        version = self.version(room["id"])
        projects = self.projects(principal, room, member)
        visibility = digest(
            [
                "room-context",
                room["id"],
                room["revision"],
                room["state"],
                version,
                principal.grant_id or principal.user_id,
                member["id"] if member else "",
                member["binding_generation"] if member else 0,
                projects,
            ]
        )
        after = read_cursor(self.c.secret, visibility, cursor) if cursor else 0
        if type(after) is not int or after < 0:
            raise DevError("INVALID_CURSOR", "上下文游标无效", 400)
        result = {
            "conversation_id": room["id"],
            "version": version,
            "visibility_revision": visibility,
            "archived": room["state"] == "archived",
            "writable": room["state"] == "active"
            and (not member or room["lead_member_id"] == member["id"]),
            "facts": [],
            "active_projects": projects,
            "current_input": None,
            "topic": None,
            "related_goals": [],
            "recent_messages": [],
            "next_cursor": None,
            "incomplete": True,
            "incomplete_reasons": [],
            "history_complete": False,
            "read_only": True,
            "read_is_consumption": False,
            "scheduled": False,
            "grants_authority": False,
        }
        reasons = set()
        scanned = after
        more = False
        positions = []
        result["active_projects_complete"] = True
        while (
            result["active_projects"]
            and len(canonical(result["active_projects"]).encode()) > byte_budget // 3
        ):
            result["active_projects"] = result["active_projects"][:-1]
            result["active_projects_complete"] = False
            reasons.add("active_projects_omitted")
        rows = self.store.all(
            """SELECT f.* FROM room_context_facts f JOIN room_context_current c ON c.fact_id=f.id
            WHERE f.conversation_id=? AND f.sequence>? ORDER BY f.sequence LIMIT 101""",
            (room["id"], after),
        )
        fact_bytes = len(canonical(result).encode()) + 512
        for row in rows[:100]:
            try:
                value = self.fact(row, principal, room, member)
            except DevError as exc:
                if exc.code not in {"CONTEXT_FACT_NOT_FOUND", "CONTEXT_SOURCE_NOT_FOUND"}:
                    reasons.add("source_needs_review")
                scanned = row["sequence"]
                continue
            if len(result["facts"]) >= limit:
                more = True
                break
            value_bytes = len(canonical(value).encode()) + (1 if result["facts"] else 0)
            if fact_bytes + value_bytes > byte_budget:
                more = True
                reasons.add("byte_budget")
                break
            fact_bytes += value_bytes
            result["facts"].append(value)
            scanned = row["sequence"]
            positions.append(scanned)
        more = more or len(rows) > 100
        if more:
            result["next_cursor"] = sign_cursor(self.c.secret, visibility, scanned)
            reasons.add("more_facts")
            if not result["facts"]:
                result["next_read"] = {
                    "cursor": result["next_cursor"],
                    "limit": 1,
                    "byte_budget": 24000,
                }
        recent = self.store.all(
            "SELECT * FROM collaboration_messages WHERE conversation_id=? ORDER BY conversation_sequence DESC LIMIT 40",
            (room["id"],),
        )
        visible = []
        for row in recent:
            try:
                self.source(self.reference(row), principal, room, member)
            except DevError:
                continue
            visible.append(row)
            if self.owner_source(row, room):
                reviewed = self.store.one(
                    "SELECT * FROM room_context_reviewed_sources WHERE conversation_id=? AND message_id=?",
                    (room["id"], row["id"]),
                )
                if (
                    not reviewed
                    or reviewed["version"] != row["version"]
                    or reviewed["digest"] != self.reference(row)["digest"]
                ):
                    reasons.add("unreviewed_input")
        # Assistant chatter must not push an unreviewed owner input outside the
        # small recent-message window and silently make the old summary current.
        pending = self.store.all(
            """SELECT m.* FROM collaboration_messages m
            LEFT JOIN room_context_reviewed_sources r ON r.conversation_id=m.conversation_id AND r.message_id=m.id
            WHERE m.conversation_id=? AND (r.message_id IS NULL OR r.version!=m.version)
                AND (m.origin='panel_owner' OR (m.origin='external_channel' AND json_extract(m.body,'$.external_origin.native_room')=1))
            ORDER BY m.conversation_sequence DESC LIMIT 101""",
            (room["id"],),
        )
        for row in pending[:100]:
            try:
                self.source(self.reference(row), principal, room, member)
            except DevError:
                continue
            if self.owner_source(row, room):
                reasons.add("unreviewed_input")
                break
        else:
            if len(pending) > 100:
                reasons.add("review_scan_incomplete")

        def preview(row):
            return {
                **self.reference(row),
                "body_text": json.loads(row["body"]).get("body_text", "")[:320],
                "text_incomplete": len(json.loads(row["body"]).get("body_text", "")) > 320,
                "read_action": "room_message_read",
            }

        enrichment = {"recent_messages": [preview(row) for row in reversed(visible[:4])]}
        if input_message_id:
            source = self.store.one(
                "SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?",
                (input_message_id, room["id"]),
            )
            if not source:
                raise DevError("CONTEXT_SOURCE_NOT_FOUND", "当前房间没有这个输入", 404)
            self.source(self.reference(source), principal, room, member)
            enrichment["current_input"] = preview(source)
            topic = self.store.one(
                "SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?",
                (source["thread_root_id"] or source["id"], room["id"]),
            )
            if topic:
                try:
                    self.source(self.reference(topic), principal, room, member)
                    enrichment["topic"] = preview(topic)
                except DevError:
                    reasons.add("topic_not_visible")
            goal_id = source.get("goal_id") or json.loads(source["body"]).get("work_id")
            if goal_id:
                goal = self.store.one(
                    "SELECT * FROM coordination_goals WHERE id=? AND conversation_id=?",
                    (goal_id, room["id"]),
                )
                try:
                    view = (
                        self.c.room_runtime.work_view(
                            goal, principal, member_id=member["id"] if member else ""
                        )
                        if goal
                        else None
                    )
                    if view:
                        enrichment["related_goals"] = [
                            {
                                k: view[k]
                                for k in (
                                    "id",
                                    "version",
                                    "state",
                                    "project_ids",
                                    "intent_revision",
                                )
                            }
                        ]
                except DevError:
                    reasons.add("goal_not_visible")
        if len(canonical({**result, **enrichment}).encode()) + 512 <= byte_budget:
            result.update(enrichment)
        else:
            reasons.add("recent_messages_omitted")
        if not version:
            reasons.add("not_summarized")
        reasons.add("history_is_paginated")
        result["incomplete_reasons"] = sorted(reasons)
        needs_review = bool(
            {"source_needs_review", "unreviewed_input", "not_summarized", "review_scan_incomplete"}
            & reasons
        )
        result["review_state"] = "needs_review" if needs_review else "current"
        for fact in result["facts"]:
            fact["review_state"] = result["review_state"]
        result["current_facts_complete"] = not more and not needs_review
        result["read_actions"] = {
            "source": "room_message_read",
            "context": "room_context",
            "update": "room_context_update",
        }
        # Enforce the caller's complete serialized budget, including cursors,
        # review markers and read descriptors, not only the fact text estimate.
        if len(canonical(result).encode()) > byte_budget:
            result.update(current_input=None, topic=None, related_goals=[], recent_messages=[])
            reasons.add("recent_messages_omitted")
            result["incomplete_reasons"] = sorted(reasons)
        while result["facts"] and len(canonical(result).encode()) > byte_budget:
            result["facts"].pop()
            positions.pop()
            result["next_cursor"] = sign_cursor(
                self.c.secret, visibility, positions[-1] if positions else after
            )
            result["current_facts_complete"] = False
            reasons.update({"byte_budget", "more_facts"})
            result["incomplete_reasons"] = sorted(reasons)
        if result["next_cursor"] and not result["facts"]:
            result["next_read"] = {
                "cursor": result["next_cursor"],
                "limit": 1,
                "byte_budget": 24000,
            }
        while result["active_projects"] and len(canonical(result).encode()) > byte_budget:
            result["active_projects"].pop()
            result["active_projects_complete"] = False
            reasons.add("active_projects_omitted")
            result["incomplete_reasons"] = sorted(reasons)
        return result
