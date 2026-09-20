#!/usr/bin/env python3
"""Build deterministic public integration assets from explicit source files."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

ROOT=Path(__file__).resolve().parents[1]
EXTENSION_FILES=('manifest.json','background.js','workspace.js','page.js','popup.html','popup.js','popup.css','idle.html')

def build():
    source=ROOT/'web/browser-extension'; buffer=io.BytesIO();entries={}
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in EXTENSION_FILES:
            path=source/name
            if path.is_symlink() or not path.is_file():raise ValueError('Missing regular extension source: '+name)
            raw=path.read_bytes();entries[name]=hashlib.sha256(raw).hexdigest()
            info=zipfile.ZipInfo(name,(2026,9,17,0,0,0));info.create_system=3
            info.external_attr=(stat.S_IFREG|0o644)<<16;info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,raw)
    manifest=json.loads((source/'manifest.json').read_text())
    if manifest.get('host_permissions'):raise ValueError('Public extension must require explicit site grants')
    guide=(ROOT/'docs/INTEGRATIONS-20260917.md').read_bytes()
    outputs={'browser-extension.zip':buffer.getvalue(),'integration-guide.md':guide}
    info={}
    for name,raw in outputs.items():
        (ROOT/'web'/name).write_bytes(raw)
        info[name]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    result={'outputs':info,'extension_sources':entries,'contains_credentials':False}
    (ROOT/'web/integration-assets.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

if __name__=='__main__':print(json.dumps(build(),indent=2))
