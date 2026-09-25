"""Synthetic install-package/auth fixtures shared by API and bootstrap tests."""
import time
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI
from hub.agent_install import make_agent_install_router
from hub.auth import Auth
from hub.store import Store
from shared.crypto import digest
from shared.util import DevError

def package_tree(root: Path):
    for directory in ('agent', 'shared', 'scripts', 'deploy'):
        (root / directory).mkdir(parents=True)
    for name in ('agent/__main__.py', 'agent/lifecycle.py', 'shared/util.py', 'shared/agent_lifecycle.py',
                 'scripts/install_agent.py', 'scripts/agent_lifecycle.py', 'scripts/helper.py'):
        (root / name).write_text('print("fixture")\n')
    (root / 'requirements-agent.txt').write_text('httpx==0\n')
    (root / 'deploy/install-from-hub.sh').write_text('#!/bin/sh\n')
    (root / 'deploy/install-from-hub.ps1').write_text('Write-Output install\n')
    (root / 'deploy/install-agent.sh').write_text('#!/bin/sh\n')
    (root / 'deploy/install-agent.ps1').write_text('Write-Output install\n')
    (root / 'deploy/start-agent.sh').write_text('#!/bin/sh\n')
    (root / 'deploy/start-agent.cmd').write_text('@echo off\n')
    (root / 'deploy/agent.service').write_text('[Service]\n')

def _real_auth_app(tmp_path, *, enabled=True):
    """Build a router with the production Auth session and CSRF checks."""
    source = tmp_path / 'source'
    package_tree(source)
    store = Store(tmp_path / 'hub')
    secret = 's' * 48
    device_id = 'd' * 32
    store.execute('INSERT INTO devices(id,name,secret,enabled,created) VALUES (?,?,?,?,?)',
                  (device_id, 'Laptop', store.encrypt(secret), int(enabled), time.time()))
    store.execute('INSERT INTO users(id,username,password_hash,created) VALUES (?,?,?,?)',
                  ('u1', 'admin', 'unused-in-session-test', time.time()))
    cookie, csrf = 'session-cookie', 'session-csrf'
    store.execute('INSERT INTO sessions(id_hash,user_id,csrf,expires) VALUES (?,?,?,?)',
                  (digest(cookie), 'u1', csrf, time.time() + 3600))
    runtime = SimpleNamespace(store=store)
    app = FastAPI()
    @app.exception_handler(DevError)
    async def dev_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, status_code=exc.status)
    app.include_router(make_agent_install_router(runtime, Auth(store), source))
    return app, store, device_id, secret, cookie, csrf
