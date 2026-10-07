"""Deterministic, opt-in HTTP probe monitoring on a long-running Hub.

Only immutable, owner-approved plans can collect. The pilot records scalar
probe observations, not production request bodies, logs, Shell or SQL results.
"""
from __future__ import annotations
import asyncio
import json
import math
import time

import httpx
from hub.principal import Principal, refresh_principal
from hub.collaboration import network
from hub.collaboration.common import INCIDENT_EVENT, STATUS_EVENT, canonical, digest, redact, timestamp, validate
from hub.collaboration.service import new_id
from shared import collaboration_contracts as contracts
from shared.util import DevError


class MonitorService:
    def __init__(self, collaboration, prober=None):
        self.c, self.store = collaboration, collaboration.store
        self.prober = prober or network.probe
        self._limit = asyncio.Semaphore(2)

    def probes_view(self, room, *, include_targets=False):
        return [{'id': row['id'], 'label': row['label'], 'created': row['created'], 'kind': 'http_get',
                 **({'target': json.loads(row['config'])} if include_targets else {})}
                for row in self.store.all('SELECT * FROM monitor_probes WHERE room_id=? ORDER BY created,id LIMIT 16', (room['id'],))]

    def register_probe(self, raw, principal):
        args = validate(contracts.ProbeRegister, raw)
        from hub.gateway.network import endpoint
        endpoint(args['url'], args['private_networks'], allow_http=args['allow_http'])
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            def save():
                self.c.live_room(room)
                if self.store.one('SELECT COUNT(*) AS n FROM monitor_probes WHERE room_id=?', (room['id'],))['n'] >= 16:
                    raise DevError('PROBE_LIMIT', '试点协作室最多登记 16 个固定探针', 409)
                identifier = new_id()
                config = {key: args[key] for key in ('url', 'private_networks', 'allow_http', 'expected_statuses', 'timeout_seconds')}
                self.store.execute('INSERT INTO monitor_probes VALUES (?,?,?,?,?)',
                                   (identifier, room['id'], redact(args['label']), canonical(config), self.c.clock()))
                return {'id': identifier, 'label': redact(args['label']), 'state': 'registered', 'collection_started': False}
            return self.c.mutation(principal, room, 'probe_register', args, save)

    def probe(self, room, identifier):
        row = self.store.one('SELECT * FROM monitor_probes WHERE id=? AND room_id=?', (identifier, room['id']))
        if not row:
            raise DevError('PROBE_NOT_FOUND', '计划只能引用当前协作室已登记的固定探针', 404)
        return row

    def check_candidate(self, room, candidate):
        expires = contracts.aware(candidate['valid_until']).timestamp()
        if not self.c.clock() < expires <= self.c.clock() + 30 * 86400:
            raise DevError('PLAN_EXPIRATION', '试点计划需要未来 30 天内的明确结束时间', 422)
        identifiers = {rule[key] for rule in candidate['rules'] for key in ('probe_id', 'require_recovery_probe')}
        if len(identifiers) > 4:
            raise DevError('PROBE_BUDGET', '首期每个计划最多使用四个独立 HTTP 探针', 422)
        probes = [self.probe(room, identifier) for identifier in identifiers]
        duration = sum(json.loads(row['config'])['timeout_seconds'] for row in probes)
        if duration > candidate['interval_seconds'] * 2:
            raise DevError('COLLECTOR_BUDGET', '采样间隔不足以容纳并发为 2 的最坏探针耗时', 422)
        if candidate['assignee_agent_id']:
            agent = self.c.agent(room, candidate['assignee_agent_id'])
            if agent['kind'] != 'work_cloud':
                raise DevError('ASSIGNEE_KIND_MISMATCH', '异常分析须分配给登记的 Work Cloud', 422)
        for rule in candidate['rules']:
            if rule['min_samples'] > rule['window_seconds'] // candidate['interval_seconds']:
                raise DevError('IMPOSSIBLE_SAMPLE_COUNT', '最小样本量超过该探针窗口理论可取得的样本数量', 422)
        return {'valid': True, 'digest': digest(candidate), 'expires_at': expires,
                'probe_ids': sorted(identifiers), 'policy_ref': 'monitor_read_only_v1',
                'collector_kind': 'hub_http_get', 'activated': False,
                'limitations': ['指标只代表登记的合成 HTTP 探针，不代表全部用户请求或主机进程健康。']}

    def validate_plan(self, raw, principal):
        args = validate(contracts.PlanValidate, raw)
        with self.store.transaction(immediate=False):
            principal, room = self.c.scope(principal, args)
            return self.check_candidate(room, args['candidate'])

    def save_plan(self, raw, principal):
        args = validate(contracts.PlanSave, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            if principal.grant_id:
                self.c.bound_worker(principal, room)
            else:
                self.c.owner(principal)
            def save():
                if principal.grant_id:
                    job = self.c.lease(principal, room, args)
                    if job['kind'] != 'propose_monitor_plan':
                        raise DevError('TASK_KIND_MISMATCH', '当前任务没有编制监控草稿的明确授权', 403)
                candidate = redact(args['candidate'])
                checked = self.check_candidate(room, candidate)
                binding = self.store.one('SELECT * FROM monitor_plan_bindings WHERE room_id=?', (room['id'],))
                latest = binding['latest_version'] if binding else 0
                if latest != args['expected_version']:
                    raise DevError('STALE_VERSION', '计划草稿已改变，请读取最新版本', 409)
                if not binding:
                    self.store.execute('INSERT INTO monitor_plan_bindings(id,room_id,collector_epoch) VALUES (?,?,?)', (new_id(), room['id'], new_id()))
                    binding = self.store.one('SELECT * FROM monitor_plan_bindings WHERE room_id=?', (room['id'],))
                version = latest + 1
                self.store.execute('INSERT INTO monitor_plans VALUES (?,?,?,?,?)', (binding['id'], version, canonical(candidate), checked['digest'], self.c.clock()))
                self.store.execute("UPDATE monitor_plan_bindings SET latest_version=?,version=version+1,state=CASE WHEN state='active' THEN state ELSE 'awaiting_approval' END WHERE id=?", (version, binding['id']))
                return {'id': binding['id'], 'plan_version': version, 'version': binding['version'] + 1,
                        'digest': checked['digest'], 'state': 'awaiting_approval', 'activated': False}
            return self.c.mutation(principal, room, 'plan_save', args, save)

    def activate_plan(self, raw, principal):
        args = validate(contracts.PlanActivate, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            def activate():
                self.c.live_room(room)
                binding = self.store.one('SELECT * FROM monitor_plan_bindings WHERE room_id=?', (room['id'],))
                if not binding or binding['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '计划生效绑定已改变', 409)
                plan = self.store.one('SELECT * FROM monitor_plans WHERE plan_id=? AND version=?', (binding['id'], args['plan_version']))
                if not plan or plan['digest'] != args['digest']:
                    raise DevError('PLAN_DIGEST_MISMATCH', '批准必须对应实际审阅的不可变计划内容', 409)
                candidate = json.loads(plan['config'])
                checked = self.check_candidate(room, candidate)
                identifier = new_id()
                self.store.execute('INSERT INTO monitor_approvals VALUES (?,?,?,?,?,?,?,?)',
                    (identifier, binding['id'], args['plan_version'], plan['digest'], principal.actor,
                     canonical(self.c.principal_snapshot(principal)), checked['expires_at'], self.c.clock()))
                self.store.execute('''UPDATE monitor_plan_bindings SET active_version=?,approval_id=?,state='active',
                    version=version+1,next_due=?,collection_fence=collection_fence+1,collection_lease_until=NULL,
                    status='awaiting_samples' WHERE id=?''', (args['plan_version'], identifier, self.c.clock(), binding['id']))
                # Existing jobs keep the old plan and must never inherit its replacement.
                self.store.execute('''UPDATE collaboration_jobs SET state='blocked',reason_code='plan_superseded',
                    fencing_token=fencing_token+1,lease_until=NULL,version=version+1,updated=?
                    WHERE room_id=? AND plan_version IS NOT NULL AND plan_version!=?
                    AND state IN ('queued','leased','running','retry_wait')''', (self.c.clock(), room['id'], args['plan_version']))
                return {'id': binding['id'], 'version': binding['version'] + 1, 'plan_version': args['plan_version'],
                        'approval_id': identifier, 'state': 'active', 'collector_enabled': self.c.config.collector_enabled,
                        'analysis_dispatch_enabled': self.c.config.analysis_dispatch_enabled}
            return self.c.mutation(principal, room, 'plan_activate', args, activate)

    def plan_view(self, room):
        binding = self.store.one('SELECT * FROM monitor_plan_bindings WHERE room_id=?', (room['id'],))
        if not binding:
            return None
        result = {key: binding[key] for key in ('id', 'latest_version', 'active_version', 'version', 'state',
            'status', 'last_collected', 'last_evaluated', 'next_due')}
        result['collector_enabled'] = self.c.config.collector_enabled
        result['collector_location'] = 'hub'
        for name, version in [('latest', binding['latest_version']), ('active', binding['active_version'])]:
            row = self.store.one('SELECT * FROM monitor_plans WHERE plan_id=? AND version=?', (binding['id'], version)) if version else None
            result[name] = {'version': row['version'], 'digest': row['digest'], 'config': json.loads(row['config'])} if row else None
        if binding['state'] == 'active':
            if not self.c.config.collector_enabled:
                result['status'] = 'collector_disabled'
            elif not binding['last_collected'] or self.c.clock() - binding['last_collected'] > max(180, result['active']['config']['interval_seconds'] * 3):
                result['status'] = 'stale' if binding['last_collected'] else 'awaiting_samples'
        result['rules'] = self.store.all('SELECT rule_id,watermark,status,opening,closing FROM monitor_rule_state WHERE plan_id=? AND plan_version=? ORDER BY rule_id', (binding['id'], binding['active_version']))
        return result

    def authorized(self, binding):
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (binding['room_id'],))
        self.c.live_room(room)
        if binding['state'] != 'active' or not binding['active_version']:
            raise DevError('PLAN_INACTIVE', '计划未激活', 409)
        approval = self.store.one('SELECT * FROM monitor_approvals WHERE id=? AND plan_id=? AND plan_version=?', (binding['approval_id'], binding['id'], binding['active_version']))
        if not approval or approval['expires_at'] <= self.c.clock():
            raise DevError('PLAN_EXPIRED', '计划批准已到期', 409)
        identity = json.loads(approval['principal'])
        principal = Principal(actor='monitor:approved-owner', user_id=identity['user_id'], scopes={'read'},
                              projects=['*'], admin=True, space_id=identity['space_id'],
                              user_epoch=identity['user_epoch'], identity_id=identity['identity_id'])
        principal = refresh_principal(self.store, principal)
        self.c.owner(principal)
        self.c.runtime.project(room['project_id'], principal)
        row = self.store.one('SELECT * FROM monitor_plans WHERE plan_id=? AND version=?', (binding['id'], binding['active_version']))
        if not row or row['digest'] != approval['digest']:
            raise DevError('PLAN_DIGEST_MISMATCH', '批准与计划内容不一致', 409)
        return room, json.loads(row['config'])

    def reserve(self):
        if not self.c.config.collector_enabled:
            return []
        now, items = self.c.clock(), []
        with self.store.transaction():
            bindings = self.store.all("SELECT * FROM monitor_plan_bindings WHERE state='active' AND next_due<=? AND (collection_lease_until IS NULL OR collection_lease_until<=?) ORDER BY next_due,id LIMIT 2", (now, now))
            for binding in bindings:
                try:
                    room, plan = self.authorized(binding)
                except DevError as exc:
                    self.store.execute("UPDATE monitor_plan_bindings SET state='paused',status=?,version=version+1,collection_fence=collection_fence+1,collection_lease_until=NULL WHERE id=?", (exc.code, binding['id']))
                    continue
                probes = sorted({rule[key] for rule in plan['rules'] for key in ('probe_id', 'require_recovery_probe')})
                generation, sequence = binding['collection_fence'] + 1, binding['sequence'] + 1
                self.store.execute('''UPDATE monitor_plan_bindings SET collection_fence=?,sequence=?,
                    collection_lease_until=?,next_due=? WHERE id=?''', (generation, sequence, now + 60, now + plan['interval_seconds'], binding['id']))
                items.append({'plan_id': binding['id'], 'plan_version': binding['active_version'], 'fence': generation,
                              'sequence': sequence, 'collector_epoch': binding['collector_epoch'],
                              'probes': [{'id': identifier, 'config': json.loads(self.probe(room, identifier)['config'])} for identifier in probes]})
        return items

    def still_authorized(self, item):
        binding = self.store.one('SELECT * FROM monitor_plan_bindings WHERE id=?', (item['plan_id'],))
        if not binding or binding['active_version'] != item['plan_version'] or binding['collection_fence'] != item['fence'] or (binding['collection_lease_until'] or 0) <= self.c.clock():
            return False
        self.authorized(binding)
        return True

    async def collect(self, item):
        async def one(probe):
            async with self._limit:
                if not await self.store.run(self.still_authorized, item):
                    return None
                started = time.monotonic()
                try:
                    reply = await self.prober(probe['config'])
                    available = reply.status in probe['config']['expected_statuses']
                    quality, latency = 'valid', (time.monotonic() - started) * 1000
                except (TimeoutError, httpx.TimeoutException):
                    available, quality, latency = False, 'timeout', None
                except DevError:
                    available, quality, latency = False, 'policy_denied', None
                except (httpx.HTTPError, OSError):
                    available, quality, latency = False, 'network_error', None
                return {'probe_id': probe['id'], 'collector_epoch': item['collector_epoch'], 'sequence': item['sequence'],
                        'collected_at': self.c.clock(), 'available': available, 'http_error': not available,
                        'latency_ms': min(latency, 60000) if latency is not None else None, 'quality': quality}
        samples = await asyncio.gather(*(one(probe) for probe in item['probes']))
        await self.store.run(self.accept, item, [sample for sample in samples if sample is not None])

    def accept(self, item, samples):
        with self.store.transaction():
            if not self.still_authorized(item):
                return {'accepted': False, 'reason_code': 'stale_collection'}
            binding = self.store.one('SELECT * FROM monitor_plan_bindings WHERE id=?', (item['plan_id'],))
            room, plan = self.authorized(binding)
            permitted = {probe['id'] for probe in item['probes']}
            for raw in samples:
                sample = validate(contracts.Sample, raw)
                if (sample['probe_id'] not in permitted or sample['collector_epoch'] != binding['collector_epoch']
                        or sample['sequence'] != item['sequence'] or sample['collected_at'] > self.c.clock() + 5):
                    raise DevError('SAMPLE_CONTEXT_MISMATCH', '采样记录不属于原采集世代与登记探针', 409)
                self.store.execute('''INSERT OR IGNORE INTO monitor_samples
                    (room_id,plan_id,plan_version,probe_id,collector_epoch,sequence,collected_at,received_at,
                     available,http_error,latency_ms,quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (room['id'], binding['id'], item['plan_version'], sample['probe_id'], sample['collector_epoch'],
                     sample['sequence'], sample['collected_at'], self.c.clock(), int(sample['available']),
                     int(sample['http_error']), sample['latency_ms'], sample['quality']))
            self.store.execute('UPDATE monitor_plan_bindings SET collection_lease_until=NULL,last_collected=? WHERE id=?', (self.c.clock() if samples else binding['last_collected'], binding['id']))
            self.evaluate(binding, room, plan)
            return {'accepted': True, 'samples': len(samples)}

    @staticmethod
    def compare(value, threshold):
        return {'gte': value >= threshold['value'], 'gt': value > threshold['value'],
                'lte': value <= threshold['value'], 'lt': value < threshold['value']}[threshold['operator']]

    def latest(self, binding, probe_id, end):
        return self.store.one('''SELECT * FROM monitor_samples WHERE plan_id=? AND plan_version=?
            AND probe_id=? AND collected_at<? ORDER BY collected_at DESC,id DESC LIMIT 1''',
            (binding['id'], binding['active_version'], probe_id, end))

    def evaluate(self, binding, room, plan):
        now = self.c.clock()
        end = math.floor(now / plan['interval_seconds']) * plan['interval_seconds']
        suppressed = any(contracts.aware(window['starts_at']).timestamp() <= now < contracts.aware(window['ends_at']).timestamp() for window in plan['maintenance']['windows'])
        if not suppressed:
            recovered = self.store.all("SELECT * FROM monitor_incidents WHERE room_id=? AND plan_id=? AND plan_version=? AND state='resolved' AND suppressed=1 LIMIT 100", (room['id'], binding['id'], binding['active_version']))
            for incident in recovered:
                self.store.execute('UPDATE monitor_incidents SET suppressed=0,version=version+1 WHERE id=?', (incident['id'],))
                self.c.emit(room, INCIDENT_EVENT, incident['id'], incident['version'] + 1,
                            {'incident_id': incident['id'], 'transition': 'recovered_after_maintenance',
                             'severity': incident['severity'], 'state': 'resolved', 'version': incident['version'] + 1,
                             'episode': incident['episode'], 'rule_id': incident['rule_id'], 'evidence_ref': incident['evidence_id']})
        statuses = []
        for rule in plan['rules']:
            previous = self.store.one('SELECT * FROM monitor_rule_state WHERE plan_id=? AND plan_version=? AND rule_id=?', (binding['id'], binding['active_version'], rule['rule_id']))
            if previous and previous['watermark'] >= end:
                statuses.append(previous['status'])
                continue
            start = end - rule['window_seconds']
            rows = self.store.all('''SELECT * FROM monitor_samples WHERE plan_id=? AND plan_version=?
                AND probe_id=? AND collected_at>=? AND collected_at<? ORDER BY collected_at,id''',
                (binding['id'], binding['active_version'], rule['probe_id'], start, end))
            latest = self.latest(binding, rule['probe_id'], end)
            recovery = self.latest(binding, rule['require_recovery_probe'], end)
            fresh = latest and now - latest['collected_at'] <= rule['sample_freshness_seconds']
            recovery_ok = bool(fresh and latest['available'] and latest['quality'] == 'valid'
                               and latest['received_at'] - latest['collected_at'] <= 2 * plan['interval_seconds']
                               and recovery and recovery['available'] and recovery['quality'] == 'valid'
                               and now - recovery['collected_at'] <= rule['sample_freshness_seconds']
                               and recovery['received_at'] - recovery['collected_at'] <= 2 * plan['interval_seconds'])
            valid = [row for row in rows if row['quality'] != 'policy_denied'
                     and row['received_at'] - row['collected_at'] <= 2 * plan['interval_seconds']]
            observations = [row['latency_ms'] for row in valid if row['latency_ms'] is not None] if rule['metric'] == 'latency_p95_ms' else valid
            if not fresh:
                status, value = 'stale', None
            elif latest['quality'] == 'policy_denied':
                status, value = 'policy_denied', None
            elif len(observations) < rule['min_samples']:
                status, value = 'insufficient_data', None
            else:
                if rule['metric'] == 'availability':
                    value = sum(row['available'] for row in valid) / len(valid)
                elif rule['metric'] == 'http_error_ratio':
                    value = sum(row['http_error'] for row in valid) / len(valid)
                else:
                    value = sorted(observations)[max(0, math.ceil(0.95 * len(observations)) - 1)]
                status = 'breaching' if self.compare(value, rule['open_when']) else ('healthy' if self.compare(value, rule['close_when']) and recovery_ok else 'observing')
            continuous = previous and end - previous['watermark'] == plan['interval_seconds']
            opening = (previous['opening'] if continuous else 0) + 1 if status == 'breaching' else 0
            closing = (previous['closing'] if continuous else 0) + 1 if status == 'healthy' else 0
            self.store.execute('''INSERT INTO monitor_rule_state VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(plan_id,plan_version,rule_id) DO UPDATE SET watermark=excluded.watermark,
                opening=excluded.opening,closing=excluded.closing,status=excluded.status''',
                (binding['id'], binding['active_version'], rule['rule_id'], end, opening, closing, status))
            statuses.append(status)
            self.update_incident(binding, room, plan, rule, value, status, opening, closing, recovery_ok, suppressed, start, end, len(observations))
        state = ('breaching' if 'breaching' in statuses else 'policy_denied' if 'policy_denied' in statuses
                 else 'stale' if 'stale' in statuses else 'insufficient_data' if 'insufficient_data' in statuses
                 else 'observing' if 'observing' in statuses else 'healthy')
        if state == 'healthy' and self.store.one("SELECT COUNT(*) AS n FROM monitor_incidents WHERE room_id=? AND state!='resolved'", (room['id'],))['n']:
            state = 'observing'
        self.store.execute('UPDATE monitor_plan_bindings SET last_evaluated=?,status=? WHERE id=?', (now, state, binding['id']))

    def update_incident(self, binding, room, plan, rule, value, status, opening, closing, recovery_ok, suppressed, start, end, sample_count):
        # A changed plan has a new evidence boundary; it cannot silently close a
        # prior version's incident. Old incidents stay visibly unverified.
        fingerprint = digest([room['id'], binding['id'], binding['active_version'], rule['rule_id'], rule['probe_id']])
        incident = self.store.one("SELECT * FROM monitor_incidents WHERE room_id=? AND fingerprint=? AND state!='resolved'", (room['id'], fingerprint))
        should_open = status == 'breaching' and opening >= rule['open_consecutive_windows']
        if incident is None and not should_open:
            return
        now, transition = self.c.clock(), None
        if incident is None:
            prior = self.store.one("SELECT * FROM monitor_incidents WHERE room_id=? AND fingerprint=? AND state='resolved' AND resolved_at>? ORDER BY resolved_at DESC LIMIT 1", (room['id'], fingerprint, now - 86400))
            if prior:
                self.store.execute("UPDATE monitor_incidents SET state='open',episode=episode+1,version=version+1,analysis_state='not_requested',resolved_at=NULL,updated=? WHERE id=?", (now, prior['id']))
                identifier, transition = prior['id'], 'reopened'
            else:
                identifier, transition = new_id(), 'opened'
                self.store.execute('''INSERT INTO monitor_incidents
                    (id,room_id,fingerprint,rule_id,probe_id,plan_id,plan_version,state,severity,opened_at,updated)
                    VALUES (?,?,?,?,?,?,?,'open',?,?,?)''',
                    (identifier, room['id'], fingerprint, rule['rule_id'], rule['probe_id'], binding['id'], binding['active_version'], rule['severity'], now, now))
            incident = self.c.object('monitor_incidents', room, identifier)
        old_state = incident['state']
        state = old_state
        if closing >= rule['close_consecutive_windows'] and recovery_ok:
            state = 'resolved'
        elif status == 'healthy':
            state = 'observing'
        elif should_open:
            state = 'open'
        if state != old_state:
            transition = 'recovered' if state == 'resolved' else state
        evidence_id = new_id()
        evidence = {'source': 'registered_http_probe', 'rule_id': rule['rule_id'], 'probe_id': rule['probe_id'],
                    'plan_version': binding['active_version'], 'metric': rule['metric'], 'value': value,
                    'window_start': timestamp(start), 'window_end': timestamp(end), 'sample_count': sample_count,
                    'status': status, 'recovery_probe_id': rule['require_recovery_probe'], 'recovery_probe_passed': recovery_ok,
                    'open_threshold': rule['open_when'], 'close_threshold': rule['close_when']}
        self.store.execute('''INSERT INTO monitor_evidence(id,room_id,incident_id,body,digest,created,expires_at)
            VALUES (?,?,?,?,?,?,?)''', (evidence_id, room['id'], incident['id'], canonical(evidence), digest(evidence), now, now + 14 * 86400))
        self.store.execute('''UPDATE monitor_incidents SET state=?,evidence_id=?,suppressed=?,version=version+1,
            updated=?,resolved_at=? WHERE id=?''', (state, evidence_id, int(suppressed), now, now if state == 'resolved' else None, incident['id']))
        current = self.c.object('monitor_incidents', room, incident['id'])
        if (transition or incident['suppressed'] and not suppressed) and not suppressed:
            self.c.emit(room, INCIDENT_EVENT, current['id'], current['version'],
                        {'incident_id': current['id'], 'transition': transition or 'maintenance_ended',
                         'severity': current['severity'], 'state': current['state'], 'version': current['version'],
                         'episode': current['episode'], 'rule_id': current['rule_id'], 'evidence_ref': evidence_id})
        if current['state'] != 'resolved' and current['analysis_state'] in {'not_requested', 'budget_exhausted', 'manual_claim_required', 'cooldown'}:
            if not self.c.config.analysis_dispatch_enabled or not plan['assignee_agent_id'] or suppressed:
                self.store.execute("UPDATE monitor_incidents SET analysis_state='manual_claim_required' WHERE id=?", (current['id'],))
                return
            previous_job = self.store.one('SELECT MAX(created) AS at FROM collaboration_jobs WHERE incident_id=?', (current['id'],))['at']
            if previous_job and now - previous_job < rule['cooldown_seconds']:
                self.store.execute("UPDATE monitor_incidents SET analysis_state='cooldown' WHERE id=?", (current['id'],))
                return
            try:
                self.c.make_job(room, agent_id=plan['assignee_agent_id'], kind='analyze_incident',
                    business_key='incident:' + current['id'] + ':' + str(current['episode']), incident_id=current['id'],
                    plan_version=binding['active_version'], context={'request': '分析已确认的合成探针异常',
                    'evidence_refs': [evidence_id], 'budget': plan['budget'], 'plan_digest': digest(plan)})
            except DevError as exc:
                analysis_state = 'budget_exhausted' if exc.code == 'BUDGET_EXCEEDED' else 'blocked'
                self.store.execute('UPDATE monitor_incidents SET analysis_state=? WHERE id=?', (analysis_state, current['id']))
            else:
                self.store.execute("UPDATE monitor_incidents SET analysis_state='queued' WHERE id=?", (current['id'],))

    def cleanup(self):
        now = self.c.clock()
        with self.store.transaction():
            self.store.execute('DELETE FROM monitor_samples WHERE id IN (SELECT id FROM monitor_samples WHERE collected_at<? LIMIT 1000)', (now - 7 * 86400,))
            # Keep reference tombstones. The latest open incident evidence remains
            # pinned, but every new evaluation replaces that one small pinned packet.
            self.store.execute("UPDATE monitor_evidence SET body='null' WHERE id IN (SELECT id FROM monitor_evidence WHERE expires_at<? AND body!='null' AND id NOT IN (SELECT evidence_id FROM monitor_incidents WHERE state!='resolved' AND evidence_id IS NOT NULL) LIMIT 200)", (now,))

    async def tick(self):
        items = await self.store.run(self.reserve)
        await asyncio.gather(*(self.collect(item) for item in items))
        await self.store.run(self.cleanup)
