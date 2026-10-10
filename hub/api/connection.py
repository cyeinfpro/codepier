"""Authenticated, read-only setup and evidence surfaces; never create access."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from hub.db_worker import database_endpoint
from hub.api.context import HubContext


def make_connection_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth

    @router.get('/api/grants/{grant_id}/connection-status')
    @database_endpoint(store)
    def status(grant_id: str, request: Request,
               project_id: str = Query(default='', max_length=100),
               workspace_id: str = Query(default='', pattern=r'^(|[a-f0-9]{32})$'),
               client_catalog_sha256: str = Query(default='', pattern=r'^(|[a-f0-9]{64})$')):
        return runtime.diagnostics.connection.status(auth.panel(request), grant_id,
            project_id=project_id, workspace_id=workspace_id,
            expected_resource=context.public_url() + '/mcp',
            client_catalog_sha256=client_catalog_sha256)

    @router.post('/api/connection/tunnel-preview')
    @database_endpoint(store)
    def tunnel_preview(request: Request, body: dict):
        # POST verifies the panel's anti-CSRF boundary even though preview has
        # no persistence, subprocess, filesystem or network side effects.
        auth.panel(request, True)
        from hub.secure_mcp_tunnel import build_preview
        return build_preview(body)

    return router
