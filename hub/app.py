"""CodePier Hub composition root. Domain behavior lives in hub.api.*."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from hub.static_assets import ReleaseAssets

from hub.auth import Auth
from hub.config import HubConfig
from hub.runtime import Runtime
from hub.store import Store
from hub.http import BodyLimit, install_http_behaviors
from hub.api.context import HubContext
from hub.api.accounts import make_accounts_router
from hub.api.devices import make_devices_router
from hub.api.projects import make_projects_router
from hub.api.activity import make_activity_router
from hub.call_log import register_call_log
from hub.api.settings import make_settings_router
from hub.api.system import make_system_router
from hub.api.models import Model, ComputerDecision, Login, DeviceCreate, DeviceUpdate, ProjectInput, ToolCall, TokenInput, PasswordInput, SettingsInput
from hub.access import make_access_router
from hub.artifacts import make_artifact_router
from hub.agent_install import make_agent_install_router
from hub.native_cli import make_native_router
from hub.vps import make_vps_router
from hub.panel_update import make_panel_update_router
from hub.integrations import CallTimingMiddleware
from hub.mcp import make_router
from hub.oauth import OAuth
from shared.panel_maintenance import PanelMaintenance, PanelMaintenanceMiddleware
from shared.instance_lock import InstanceLock
from shared.util import VERSION, normalize_url

BASE = Path(__file__).resolve().parent.parent


def create_app(data_dir: str | None = None):
    config = HubConfig.from_env()
    directory = Path(data_dir or os.getenv("HUB_DATA_DIR", str(BASE / "data"))).resolve()
    instance_lock = InstanceLock(directory / ".hub.lock")
    store = None
    try:
        store = Store(directory)
        runtime, auth = Runtime(store), Auth(store)
        def public_url():
            row = store.one("SELECT value FROM meta WHERE key='public_url'")
            return normalize_url(row["value"] if row else config.public_url)

        @asynccontextmanager
        async def lifespan(app):
            try:
                await runtime.start()
                yield
            finally:
                try:
                    await runtime.stop()
                finally:
                    try:
                        await store.aclose()
                    finally:
                        instance_lock.close()

        app = FastAPI(title="CodePier Agent", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
        app.state.store, app.state.runtime, app.state.auth, app.state.config = store, runtime, auth, config
        maintenance = PanelMaintenance(runtime, os.getenv("HUB_PANEL_UPDATE_SOCKET", ""))
        runtime.panel_maintenance = maintenance
        app.add_middleware(BodyLimit)
        app.add_middleware(PanelMaintenanceMiddleware, gate=maintenance)
        app.add_middleware(CallTimingMiddleware, runtime=runtime)
        install_http_behaviors(app)
        context = HubContext(store, runtime, auth, config, maintenance, public_url, BASE)
        for make in (make_accounts_router, make_devices_router, make_projects_router,
                     make_activity_router, make_settings_router, make_system_router):
            app.include_router(make(context))
        app.include_router(OAuth(auth, runtime, public_url).router)
        app.include_router(make_router(auth, runtime, public_url))
        for make in (make_artifact_router, make_access_router, make_native_router,
                     make_vps_router, make_panel_update_router):
            app.include_router(make(auth, runtime))
        app.include_router(make_agent_install_router(runtime, auth))
        register_call_log(app, runtime, auth)
        app.mount("/static", ReleaseAssets(directory=BASE / "web"), name="static")
        return app
    except BaseException:
        try:
            if store is not None:
                store.close()
        finally:
            instance_lock.close()
        raise
