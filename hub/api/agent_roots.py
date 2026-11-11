"""Manage a node's allowed roots through authenticated panel control messages."""

from fastapi import APIRouter, Request, Path

from hub.api.context import HubContext
from hub.agent_roots import authorize, availability, device_project, receipt
from hub.db_worker import database_endpoint
from shared.agent_roots_contracts import RootsChange, RootsCommand, RootsGet


def make_agent_roots_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth

    def resolve(identifier, request, write=False):
        principal = auth.admin(request, write)
        return authorize(runtime, principal, identifier)

    async def dispatch(identifier, request, tool, body=None):
        principal, _ = await store.run(resolve, identifier, request, body is not None)
        project = await store.run(device_project, runtime, principal, identifier)
        args = (
            RootsGet(device_id=identifier).model_dump()
            if body is None
            else RootsCommand(device_id=identifier, **body.model_dump()).model_dump()
        )
        # Preview and apply must never share the operation key: preview cannot
        # consume the idempotency identity reserved for the actual save.
        if tool == "agent_roots_preview":
            # The Agent schema requires a request identity, independent from
            # the durable Hub save key. A fresh bounded preview uses its own ID.
            import uuid

            args["idempotency_key"] = "roots-preview-" + uuid.uuid4().hex
        value = await runtime.dispatch(tool, args, project, principal)
        await store.run(resolve, identifier, request, body is not None)
        return value

    @router.get("/api/devices/{identifier}/roots")
    async def get_roots(identifier: str, request: Request):
        principal, device = await store.run(resolve, identifier, request)
        state = await store.run(availability, runtime, device)
        if state != "supported":
            return {
                "device_id": identifier,
                "state": state,
                "can_save": False,
                "config_sha256": None,
                "roots": [],
                "observed_at": None,
                "observed_config": False,
            }
        return await dispatch(identifier, request, "agent_roots_get")

    @router.post("/api/devices/{identifier}/roots/preview")
    async def preview_roots(identifier: str, request: Request, body: RootsChange):
        return await dispatch(identifier, request, "agent_roots_preview", body)

    @router.post("/api/devices/{identifier}/roots")
    async def save_roots(identifier: str, request: Request, body: RootsChange):
        return await dispatch(identifier, request, "agent_roots_set", body)

    @router.get("/api/devices/{identifier}/roots/requests/{key}")
    @database_endpoint(store)
    def roots_receipt(
        identifier: str,
        request: Request,
        key: str = Path(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
    ):
        principal, _ = resolve(identifier, request)
        return receipt(runtime, principal, identifier, key)

    return router
