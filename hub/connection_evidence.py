"""Evidence-backed MCP onboarding; observations never confer authority."""
from __future__ import annotations

import hashlib
import json
import time

from hub.access_preview import grant_access_preview
from shared.mcp_protocol import SERVER_INFO, SUPPORTED
from shared.tool_protocol import CONTRACT_PROTOCOL, wire_version
from shared.util import DevError

STAGES = frozenset({'authentication', 'discovery', 'catalog', 'readonly', 'call'})
FRESH_SECONDS = 600


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


class ConnectionEvidence:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.write_errors = 0
        self._authentication_observed = {}
        self._writes_since_prune = 0
        self.store.execute("""CREATE TABLE IF NOT EXISTS mcp_connection_evidence (
            space_id TEXT NOT NULL, user_id TEXT NOT NULL, grant_id TEXT NOT NULL,
            stage TEXT NOT NULL, project_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            binding TEXT NOT NULL, mapping TEXT NOT NULL, observed REAL NOT NULL,
            detail TEXT NOT NULL,
            PRIMARY KEY(space_id,user_id,grant_id,stage,project_id,workspace_id))""")

    def binding(self, grant):
        from hub.access import grant_revision
        profile = self.store.one('SELECT version,enabled FROM access_profiles WHERE id=? AND user_id=? AND space_id=?',
                                 (grant.get('profile_id'), grant['user_id'], grant['space_id']))
        role = self.store.one('SELECT version,enabled FROM access_roles WHERE id=? AND space_id=?',
                              (grant.get('role_id'), grant['space_id']))
        return digest([dict(grant), grant_revision(self.store, grant['id']), profile, role])

    def observe(self, principal, stage, *, project=None, workspace_id='', catalog_sha256=None,
                tool=None, operation_id=None, catalog_has_more=None, completed_at=None):
        """Only validated server code supplies stages; no client-reported success."""
        if stage not in STAGES or not principal.grant_id:
            return
        try:
            key = (principal.space_id, principal.user_id, principal.grant_id)
            if stage == 'authentication' and time.monotonic() - self._authentication_observed.get(key, 0) < 30:
                return  # Observation throttling only; live authorization already ran.
            grant = self.store.one('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=? AND revoked=0',
                                   (principal.grant_id, principal.user_id, principal.space_id))
            if not grant:
                return
            detail = {'server_info': dict(SERVER_INFO), 'contract_protocol': CONTRACT_PROTOCOL}
            if isinstance(catalog_sha256, str) and len(catalog_sha256) == 64:
                detail['catalog_sha256'] = catalog_sha256
            if tool:
                detail['tool'] = tool
                detail['tool_contract_version'] = wire_version(tool)
            if operation_id:
                detail['operation_id'] = operation_id
            if type(catalog_has_more) is bool:
                detail['catalog_has_more'] = catalog_has_more
            self.store.execute("""INSERT INTO mcp_connection_evidence VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(space_id,user_id,grant_id,stage,project_id,workspace_id)
                DO UPDATE SET binding=excluded.binding,mapping=excluded.mapping,
                observed=excluded.observed,detail=excluded.detail
                WHERE excluded.observed >= mcp_connection_evidence.observed""",
                (principal.space_id, principal.user_id, principal.grant_id, stage,
                 project['id'] if project else '', workspace_id if project else '',
                 self.binding(grant), digest([project['root'], project['device_id']]) if project else '',
                 min(time.time(), completed_at) if type(completed_at) in {int, float} else time.time(), json.dumps(detail)))
            if stage == 'authentication':
                self._authentication_observed[key] = time.monotonic()
                if len(self._authentication_observed) > 4096:
                    self._authentication_observed.pop(next(iter(self._authentication_observed)))
            self._writes_since_prune += 1
            if self._writes_since_prune >= 64:
                self.store.execute("""DELETE FROM mcp_connection_evidence WHERE rowid IN
                    (SELECT rowid FROM mcp_connection_evidence ORDER BY observed DESC LIMIT -1 OFFSET 4096)""")
                self._writes_since_prune = 0
        except Exception:
            # Telemetry failure must never alter the tool's outcome or authority.
            self.write_errors += 1

    def returned(self, principal, name, arguments, value):
        """A returned tool call is evidence of the contract, never of host scanning."""
        self.observe(principal, 'call', tool=name)

    def completed(self, operation, result, *, state, completed_at):
        """Use the old in-memory request after the real terminal result commits.

        Runtime clears encrypted payload at completion. Never retain that payload
        or infer a historical mapping from the current project when polling.
        """
        if (operation['tool'] != 'read' or state != 'succeeded' or not operation['grant_id']
                or result.get('ok') is not True):
            return
        try:
            grant = self.store.one('SELECT * FROM grants WHERE id=? AND revoked=0',
                                   (operation['grant_id'],))
            if not grant:
                return
            principal = self.runtime.grant_principal(grant)
            row = self.runtime.operation_row(operation['id'], principal)
            if row['grant_id'] != principal.grant_id or row['state'] != 'succeeded':
                return
            project = self.runtime.project(row['project_id'], principal)
            request = json.loads(self.store.decrypt(operation['payload'])) if operation.get('payload') else {}
            original_project = request.get('project') or {}
            if original_project.get('root') != project['root'] or row['device_id'] != project['device_id']:
                return
            data = result.get('data') or {}
            if data.get('batch') is True and not any(item.get('ok') is True and 'content' in item
                                                    for item in data.get('files', [])):
                return
            original = json.loads(operation['args_summary'] or '{}')
            self.observe(principal, 'readonly', project=project,
                workspace_id=original.get('workspace_id', ''), tool='read', operation_id=row['id'],
                completed_at=completed_at)
        except DevError:
            # A current denial is not a telemetry failure and grants no evidence.
            return
        except Exception:
            self.write_errors += 1

    def status(self, viewer, grant_id, *, project_id='', workspace_id='', expected_resource=None,
               client_catalog_sha256=''):
        preview = grant_access_preview(self.store, viewer, grant_id, expected_resource=expected_resource)
        grant = self.store.one('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?',
                              (grant_id, viewer.user_id, viewer.space_id))
        allowed = {p['id']: p for p in preview['projects']}
        if project_id and project_id not in allowed:
            raise DevError('NOT_FOUND', '项目不存在或当前会话不可见', 404)
        if workspace_id and not project_id:
            raise DevError('INVALID_ARGUMENTS', '隔离目录必须绑定项目')
        project = self.store.one('SELECT * FROM projects WHERE id=? AND space_id=?',
                                (project_id, viewer.space_id)) if project_id else None
        # Workspace evidence is only surfaced after existing current ownership checks.
        if workspace_id:
            subject = self.runtime.project(project_id, viewer)
            receipt = self.runtime.operation_row(workspace_id, viewer)
            if (receipt['tool'] != 'worktrees_create' or receipt['project_id'] != subject['id']
                    or receipt['grant_id'] != grant_id or receipt['state'] != 'succeeded'):
                raise DevError('NOT_FOUND', '未找到当前连接有权查看的隔离目录创建回执', 404)
            created = json.loads(receipt['result'] or '{}').get('data') or {}
            if created.get('workspace_id') != workspace_id:
                raise DevError('NOT_FOUND', '隔离目录回执绑定不一致', 404)
        now, binding = time.time(), self.binding(grant)
        mapping = digest([project['root'], project['device_id']]) if project else ''
        rows = self.store.all("""SELECT * FROM mcp_connection_evidence
            WHERE space_id=? AND user_id=? AND grant_id=?
            AND ((project_id='' AND workspace_id='') OR (project_id=? AND workspace_id=?)) ORDER BY observed""",
            (viewer.space_id, viewer.user_id, grant_id, project_id, workspace_id))
        evidence = {}
        for row in rows:
            current = row['binding'] == binding and (not row['project_id'] or row['mapping'] == mapping)
            age = max(0, round(now - row['observed']))
            evidence[row['stage']] = {**json.loads(row['detail']), 'observed_at': row['observed'],
                'age_seconds': age, 'freshness': 'fresh' if current and age <= FRESH_SECONDS else 'stale',
                'binding_current': current, 'source': 'server_observation'}
        active = preview['grant']['state'] == 'active'
        read_allowed = bool(project and allowed[project_id]['actions']['read']['allowed'])
        labels = {'authentication': '有效授权', 'discovery': 'MCP 服务发现',
                  'catalog': '工具目录返回', 'readonly': '实际只读调用'}
        layers = []
        for stage, label in labels.items():
            item = evidence.get(stage)
            state = 'observed' if item and item['freshness'] == 'fresh' else 'stale' if item else 'unknown'
            if stage == 'authentication':
                state = 'allowed' if active else 'blocked'
            if stage == 'readonly' and project and not read_allowed:
                state = 'blocked'
            layers.append({'id': stage, 'label': label, 'status': state, 'evidence': item,
                           'checked_at': now if stage == 'authentication' else None})
        online = bool(project and self.runtime.online(project['device_id']))
        device = self.store.one('SELECT enabled,last_seen,info FROM devices WHERE id=? AND space_id=?',
                                (project['device_id'], viewer.space_id)) if project and read_allowed else None
        info = json.loads(device['info']) if device else {}
        capabilities = info.get('capabilities')
        layers.append({'id': 'agent', 'label': 'Agent 当前可用性',
            'status': 'online' if device and device['enabled'] and online else 'offline' if device else 'unknown',
            'checked_at': now, 'observed_at': device['last_seen'] if device else None,
            'read_capability': 'available' if isinstance(capabilities, list) and 'read' in capabilities else
                               'unavailable' if isinstance(capabilities, list) else 'unknown',
            'note': '在线和声明能力不证明某条路径或执行任务获准；以实际调用结果为准。'})
        current_hash = None
        if active:
            from hub.tool_router import ToolRouter
            from hub.gateway.catalog import fingerprint
            try:
                subject = self.runtime.grant_principal(grant)
                _, native, external = ToolRouter(self.store, self.runtime.gateway).definitions(subject)
                current_hash = fingerprint(sorted(native + external, key=lambda item: item['name']))
            except DevError:
                pass
        catalog = evidence.get('catalog')
        observed_hash = catalog.get('catalog_sha256') if catalog and catalog['binding_current'] else None
        if current_hash and observed_hash and current_hash != observed_hash:
            for layer in layers:
                if layer['id'] == 'catalog':
                    layer['status'] = 'stale'
        last_call = evidence.get('call')
        return {'checked_at': now, 'hub_mcp_url': expected_resource, 'owner': preview['owner'],
            'space': preview['space'], 'grant': preview['grant'], 'profile': preview['profile'],
            'role': preview['role'], 'projects': preview['projects'],
            'project_id': project_id or None, 'workspace_id': workspace_id or None,
            'layers': layers, 'server_info': dict(SERVER_INFO), 'contract_protocol': CONTRACT_PROTOCOL,
            'supported_protocols': SUPPORTED, 'last_actual_call': last_call,
            'catalog': {'scope': 'effective_connection', 'current_effective_sha256': current_hash,
                'last_served_sha256': observed_hash,
                'last_served_matches_current': current_hash == observed_hash if current_hash and observed_hash else None,
                'client_reported_sha256': client_catalog_sha256 or None,
                'client_report_matches_last_served': (client_catalog_sha256 == observed_hash)
                    if client_catalog_sha256 and observed_hash else None,
                'client_cache_status': 'unknown', 'host_scan_status': 'unknown',
                'auto_refresh_supported': False,
                'note': '服务器只能确认返回了目录页面；不能观测 ChatGPT 是否扫描完成、启用工具或刷新缓存。'},
            'next_actions': ['核对当前 Hub、Space、连接所有者与有效项目授权。',
                '在客户端添加或刷新连接，核对实际工具目录；发现记录不等于宿主扫描完成。',
                '在新对话启用 CodePier，仅列出项目并读取已授权测试文件；核对原 operation_id 和结果。',
                '发生拒绝或撤权请停止；断线或回执不明时查询原 operation_id，不要新键重复执行。'],
            'diagnostic_write_errors': self.write_errors}
