"""Serve verified release assets with immutable content identities.

Only generated version+SHA URLs are immutable. Legacy/unversioned URLs always
revalidate. Verified bytes are frozen at startup so an on-disk edit cannot make
an immutable URL serve different content between requests.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re

from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from shared.util import VERSION


class ReleaseAssets(StaticFiles):
    def __init__(self, *, directory):
        super().__init__(directory=directory, follow_symlink=False)
        root = Path(directory).resolve()
        manifest = root / 'assets-manifest.json'
        self.assets: dict[str, tuple[str, bytes, str]] = {}
        if not manifest.exists() and not manifest.is_symlink():
            # Legacy installation/test harnesses keep revalidation semantics.
            return
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 256 * 1024:
            raise ValueError('Invalid panel asset manifest')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != VERSION or data.get('algorithm') != 'sha256' or not isinstance(data.get('assets'), dict):
            raise ValueError('Panel asset manifest does not match this release')
        total = 0
        for name, entry in data['assets'].items():
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or '..' in path.parts or str(path) != name
                    or '\\' in name or '%' in name or not isinstance(entry, dict)
                    or not re.fullmatch(r'[a-f0-9]{64}', str(entry.get('sha256', '')))):
                raise ValueError('Invalid panel asset identity')
            file = root.joinpath(*path.parts)
            if any(root.joinpath(*path.parts[:index]).is_symlink() for index in range(1, len(path.parts) + 1)):
                raise ValueError('Symlinked panel assets are not supported')
            if not file.is_file() or file.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('Missing or oversized panel asset: ' + name)
            with file.open('rb') as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            digest = hashlib.sha256(raw).hexdigest()
            total += len(raw)
            if len(raw) != entry.get('bytes') or digest != entry['sha256'] or total > 16 * 1024 * 1024:
                raise ValueError('Panel asset content does not match its inventory: ' + name)
            media = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            self.assets[name] = (digest, raw, media)

    async def get_response(self, path, scope):
        request = Request(scope)
        hashes = request.query_params.getlist('h')
        if not hashes:
            response = await super().get_response(path, scope)
            response.headers['Cache-Control'] = 'no-cache'
            return response
        entry = self.assets.get(path)
        versions = request.query_params.getlist('v')
        if (scope['method'] not in {'GET', 'HEAD'} or len(hashes) != 1
                or versions != ['codepier-' + VERSION] or entry is None or hashes[0] != entry[0]):
            return Response(status_code=404, headers={'Cache-Control': 'no-store'})
        digest, raw, media = entry
        etag = '"sha256:' + digest + '"'
        headers = {'Cache-Control': 'public, max-age=31536000, immutable', 'ETag': etag}
        candidates = [value.strip().removeprefix('W/') for value in request.headers.get('if-none-match', '').split(',')]
        if etag in candidates or '*' in candidates:
            return Response(status_code=304, headers=headers)
        headers['Content-Length'] = str(len(raw))
        return Response(raw if scope['method'] == 'GET' else b'', media_type=media, headers=headers)
