"""Synthetic regressions for audit P1/P7/P8/P9/P15."""
import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

from hub.auth import Auth
from hub.store import Store
from hub.store_contract import TransactionAborted
from shared.crypto import password_hash, password_verify, password_needs_rehash
from shared.util import DevError
from scripts.panel_update_runtime import (DockerRuntime, OAUTH_OLD_DEFAULT,
    OAUTH_CURRENT_DEFAULT, migrate_oauth_redirect_hosts, atomic_json, fingerprint, read_json)
from tests.test_bug_report_auth_20261020 import request
from tests.test_panel_update_runtime import deployment, DockerBoundary


def legacy_hash(password):
    salt = b'0' * 16
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f'scrypt$16384$8$1${salt.hex()}${value.hex()}'


def test_password_upgrade_and_legacy_verification():
    old, new = legacy_hash('synthetic'), password_hash('synthetic')
    assert password_verify('synthetic', old) and password_needs_rehash(old)
    assert password_verify('synthetic', new) and not password_needs_rehash(new)
    assert not password_verify('wrong', old) and not password_verify('wrong', new)


@pytest.mark.parametrize('value', [None, '', 'scrypt$999999999$8$1$00$00',
    'scrypt$32768$99999$1$00$00', 'scrypt$32768$8$9999$00$00',
    'wrong$32768$8$1$00$00', 'scrypt$-1$8$1$00$00'])
def test_malformed_hash_rejected_before_scrypt(value, monkeypatch):
    monkeypatch.setattr(hashlib, 'scrypt', lambda **kw: pytest.fail('unbounded hash admitted'))
    assert not password_verify('synthetic', value)


def test_login_rehash_and_ip_budget_cannot_be_reset(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        store.execute('INSERT INTO users VALUES (?,?,?,?)', ('owner', 'owner', legacy_hash('synthetic'), 1))
        auth = Auth(store)
        for i in range(7):
            auth._login_attempt(request(), f'victim-{i}')
        asyncio.run(auth.login(request(), 'owner', 'synthetic'))
        encoded = store.one("SELECT password_hash FROM users WHERE id='owner'")['password_hash']
        assert not password_needs_rehash(encoded) and password_verify('synthetic', encoded)
        assert store.one("SELECT count(*) AS n FROM login_attempts")['n'] == 7
        auth._login_attempt(request(), 'victim-next')
        with pytest.raises(DevError) as failure:
            auth._login_attempt(request(), 'victim-over-limit')
        assert failure.value.code == 'LOGIN_THROTTLED'
    finally:
        store.close()


def test_rehash_cannot_overwrite_concurrent_password_change(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        old = legacy_hash('synthetic')
        store.execute('INSERT INTO users VALUES (?,?,?,?)', ('owner', 'owner', old, 1))
        auth = Auth(store)
        _, user = auth._login_attempt(request(), 'owner')
        replacement = password_hash('replacement')
        store.execute("UPDATE users SET password_hash=? WHERE id='owner'", (replacement,))
        with pytest.raises(DevError):
            auth._login_session(user, '192.0.2.1', 'owner', password_hash('synthetic'))
        assert store.one("SELECT password_hash FROM users WHERE id='owner'")['password_hash'] == replacement
        assert not store.one('SELECT * FROM sessions')
    finally:
        store.close()


@pytest.mark.parametrize('legacy_context', [False, True])
def test_execute_audit_and_nested_transaction_never_commit_outer(tmp_path, legacy_context):
    store = Store(tmp_path / 'hub')
    callbacks = []
    try:
        with store.lock:
            context = store.db if legacy_context else store.transaction()
            with pytest.raises(RuntimeError, match='injected'):
                with context:
                    store.execute("INSERT INTO meta VALUES ('audit-test','outer')")
                    with store.transaction():
                        store.audit('fixture', 'audit.rollback')
                        store.after_commit(lambda: callbacks.append('committed'))
                    with sqlite3.connect(store.state_directory / 'hub.sqlite3') as observer:
                        assert observer.execute("SELECT value FROM meta WHERE key='audit-test'").fetchone() is None
                    raise RuntimeError('injected')
        assert not store.one("SELECT value FROM meta WHERE key='audit-test'")
        assert not store.one("SELECT * FROM audit WHERE action='audit.rollback'")
        assert callbacks == []
    finally:
        store.close()


def test_caught_nested_failure_keeps_outer_rollback_only(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        with pytest.raises(TransactionAborted):
            with store.transaction():
                store.execute("INSERT INTO meta VALUES ('audit-test','outer')")
                try:
                    with store.transaction():
                        store.audit('fixture', 'audit.rollback')
                        raise RuntimeError('injected')
                except RuntimeError:
                    pass
        assert not store.one("SELECT value FROM meta WHERE key='audit-test'")
    finally:
        store.close()


@pytest.mark.parametrize('quote', ['', '"', "'"])
@pytest.mark.parametrize('newline', ['', '\n', '\r\n'])
def test_oauth_migration_preserves_quote_and_newline(quote, newline):
    before = f'OAUTH_REDIRECT_HOSTS={quote}{OAUTH_OLD_DEFAULT}{quote}{newline}'.encode()
    after, changed = migrate_oauth_redirect_hosts(before)
    assert changed and after == before.replace(OAUTH_OLD_DEFAULT.encode(), OAUTH_CURRENT_DEFAULT.encode())
    assert migrate_oauth_redirect_hosts(after) == (after, False)


@pytest.mark.parametrize('value', ['custom.example', OAUTH_CURRENT_DEFAULT, OAUTH_OLD_DEFAULT + ',custom.example'])
def test_oauth_migration_preserves_custom_policy(value):
    before = f'# preserve\nOAUTH_REDIRECT_HOSTS={value}\nSECRET=unchanged\n'.encode()
    assert migrate_oauth_redirect_hosts(before) == (before, False)
    assert migrate_oauth_redirect_hosts(b'SECRET=unchanged\n') == (b'SECRET=unchanged\n', False)


def test_oauth_migration_updates_frozen_target_not_rollback(tmp_path):
    home, config, job, spec = deployment(tmp_path)
    spec['services']['hub']['environment']['OAUTH_REDIRECT_HOSTS'] = OAUTH_OLD_DEFAULT
    before = f'OAUTH_REDIRECT_HOSTS={OAUTH_OLD_DEFAULT}\n'.encode()
    (tmp_path / '.env').write_bytes(before)
    current = read_json(home / 'current.json')
    current['compose'] = spec
    current['files']['.env'] = fingerprint(tmp_path / '.env')
    atomic_json(home / 'current.json', current)
    runtime = DockerRuntime(tmp_path, home, config, DockerBoundary(tmp_path, spec))
    source = tmp_path / 'source'; source.mkdir()
    job.update(runtime.prepare(job, source))
    directory = home / 'jobs' / job['id']
    assert read_json(directory / 'old.json')['services']['hub']['environment']['OAUTH_REDIRECT_HOSTS'] == OAUTH_OLD_DEFAULT
    assert read_json(directory / 'target.json')['services']['hub']['environment']['OAUTH_REDIRECT_HOSTS'] == OAUTH_CURRENT_DEFAULT
    assert (directory / 'env.before').read_bytes() == before
    assert OAUTH_CURRENT_DEFAULT.encode() in runtime.committed_env(job, before)
    runtime.committed_env(job, before)
    assert job['notes'].count('oauth_redirect_hosts_migrated') == 1


def test_k3s_forwarded_trust_is_restricted_to_traefik():
    import yaml
    manifests = list(yaml.safe_load_all(Path('deploy/k3s/deployment.yaml').read_text()))
    policy = next(item for item in manifests if item and item['kind'] == 'NetworkPolicy')
    spec = policy['spec']
    assert spec['policyTypes'] == ['Ingress']
    source = spec['ingress'][0]['from'][0]
    assert source['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'] == 'kube-system'
    assert source['podSelector']['matchLabels']['app.kubernetes.io/name'] == 'traefik'
    assert spec['ingress'][0]['ports'] == [{'protocol': 'TCP', 'port': 8765}]


@pytest.mark.parametrize('caught_inside', [False, True])
def test_savepoint_isolates_nested_failure_and_callbacks(tmp_path, caught_inside):
    store = Store(tmp_path / 'hub')
    callbacks = []
    try:
        with store.transaction():
            store.execute("INSERT INTO meta VALUES ('before-savepoint','kept')")
            store.after_commit(lambda: callbacks.append('before'))
            with pytest.raises((RuntimeError, TransactionAborted)):
                with store.savepoint():
                    try:
                        with store.transaction():
                            store.execute("INSERT INTO meta VALUES ('inside-savepoint','discarded')")
                            store.after_commit(lambda: callbacks.append('discarded'))
                            raise RuntimeError('isolated failure')
                    except RuntimeError:
                        if not caught_inside:
                            raise
            store.execute("INSERT INTO meta VALUES ('after-savepoint','kept')")
            store.after_commit(lambda: callbacks.append('after'))
        assert callbacks == ['before', 'after']
        assert store.one("SELECT value FROM meta WHERE key='before-savepoint'")
        assert store.one("SELECT value FROM meta WHERE key='after-savepoint'")
        assert not store.one("SELECT value FROM meta WHERE key='inside-savepoint'")
    finally:
        store.close()


def test_savepoint_does_not_clear_prior_rollback_only(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        with pytest.raises(TransactionAborted):
            with store.transaction():
                store.execute("INSERT INTO meta VALUES ('outer-failure','discarded')")
                try:
                    with store.transaction():
                        raise RuntimeError('prior failure')
                except RuntimeError:
                    pass
                with pytest.raises(RuntimeError):
                    with store.savepoint():
                        raise RuntimeError('isolated failure')
        assert not store.one("SELECT value FROM meta WHERE key='outer-failure'")
        with pytest.raises(RuntimeError, match='active'):
            with store.savepoint():
                pass
    finally:
        store.close()


def test_savepoint_success_waits_for_outer_commit(tmp_path):
    store = Store(tmp_path / 'hub')
    callbacks = []
    try:
        with store.transaction():
            with store.savepoint():
                store.execute("INSERT INTO meta VALUES ('savepoint-success','kept')")
                store.after_commit(lambda: callbacks.append('committed'))
            assert callbacks == []
            with sqlite3.connect(store.state_directory / 'hub.sqlite3') as observer:
                assert observer.execute("SELECT value FROM meta WHERE key='savepoint-success'").fetchone() is None
        assert callbacks == ['committed']
    finally:
        store.close()
