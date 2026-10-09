"""Instance-admin-only, CSRF-protected file ingress settings and exact previews."""
from fastapi import APIRouter, Request
from hub.db_worker import database_endpoint
from hub.file_import_settings import FileImportPreview, FileImportUpdate, apply, preview, snapshot

def make_file_import_settings_router(context):
    router = APIRouter()
    store, auth = context.store, context.auth

    @router.get("/api/settings/file-import")
    @database_endpoint(store)
    def read(request: Request):
        with store.transaction():
            auth.instance(request)
            return snapshot(store)

    @router.post("/api/settings/file-import/preview")
    @database_endpoint(store)
    def review(request: Request, body: FileImportPreview):
        auth.instance(request, True)
        with store.transaction():
            auth.instance(request, True)
            result, _ = preview(store, body.expected_revision, body.patch)
            return result

    @router.put("/api/settings/file-import")
    @database_endpoint(store)
    def update(request: Request, body: FileImportUpdate):
        auth.instance(request, True)
        with store.transaction():
            principal = auth.instance(request, True)
            return apply(store, principal, body)

    return router
