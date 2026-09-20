"""Resume an authenticated artifact download, validate SHA-256, publish without overwrite."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import time
from pathlib import Path
import httpx
from scripts.mcp_stdio_bridge import token_from_file
from shared.util import normalize_url, atomic_json


def download(hub,identifier,token_file,output,attempts=6):
    hub=normalize_url(hub);output=Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():raise ValueError('Output already exists; choose a new filename')
    output.parent.mkdir(parents=True,exist_ok=True)
    partial=output.with_name(output.name+'.part');state=output.with_name(output.name+'.part.json')
    if partial.is_symlink() or state.is_symlink():raise ValueError('Partial download paths must not be symlinks')
    with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as client:
        def headers():return {'Authorization':'Bearer '+token_from_file(Path(token_file))}
        response=client.get(hub+'/api/artifacts/'+identifier,headers=headers());response.raise_for_status();meta=response.json()
        identity={k:meta[k] for k in ('artifact_id','sha256','bytes')}
        if identity['artifact_id']!=identifier:raise ValueError('Wrong artifact identity')
        if partial.exists():
            if not state.exists() or json.loads(state.read_text())!=identity:raise ValueError('Existing partial file belongs to a different artifact')
        else:
            if state.exists() and json.loads(state.read_text())!=identity:raise ValueError('Partial metadata belongs to a different artifact')
            atomic_json(state,identity)
        flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
        descriptor=os.open(partial,flags,0o600)
        try:
            import stat
            info=os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>identity['bytes']:
                raise ValueError('Invalid partial file')
            with os.fdopen(descriptor,'r+b',closefd=False) as stream:
                for attempt in range(attempts):
                    offset=stream.seek(0,os.SEEK_END)
                    if offset==identity['bytes']:break
                    request_headers=headers()
                    if offset:request_headers.update({'Range':f'bytes={offset}-','If-Range':'"'+identity['sha256']+'"'})
                    try:
                        with client.stream('GET',hub+'/api/artifacts/'+identifier+'/download',headers=request_headers) as result:
                            result.raise_for_status()
                            if result.headers.get('etag')!='"'+identity['sha256']+'"':raise ValueError('Artifact ETag changed')
                            if offset and (result.status_code!=206 or result.headers.get('content-range')!=f"bytes {offset}-{identity['bytes']-1}/{identity['bytes']}"):
                                raise ValueError('Server did not honor resume range; refusing to append a full response')
                            if not offset and result.status_code!=200:raise ValueError('Unexpected download status')
                            for chunk in result.iter_bytes(256*1024):
                                if stream.tell()+len(chunk)>identity['bytes']:raise ValueError('Response exceeds declared artifact size')
                                stream.write(chunk)
                        stream.flush();os.fsync(stream.fileno())
                        if stream.tell()!=identity['bytes']:raise httpx.ReadError('Incomplete download')
                    except (httpx.TransportError,httpx.HTTPStatusError) as exc:
                        stream.flush();os.fsync(stream.fileno())
                        status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
                        if attempt+1==attempts or status is not None and status not in {408,429,500,502,503,504}:raise
                        time.sleep(min(4,.5*2**attempt))
                stream.seek(0);checksum=hashlib.sha256()
                for part in iter(lambda:stream.read(1024*1024),b''):checksum.update(part)
                if os.fstat(descriptor).st_size!=identity['bytes'] or checksum.hexdigest()!=identity['sha256']:
                    raise ValueError('Final SHA-256 verification failed; partial file retained, not published')
            # Atomic no-overwrite publication. Retain partial files on any failure.
            os.link(partial,output,follow_symlinks=False)
            partial.unlink();state.unlink(missing_ok=True)
            return {**identity,'output':str(output),'verified':True}
        finally:os.close(descriptor)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hub',required=True);parser.add_argument('--artifact-id',required=True)
    parser.add_argument('--token-file',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:print(json.dumps(download(args.hub,args.artifact_id,args.token_file,args.output),ensure_ascii=False))
    except (ValueError,OSError,httpx.HTTPError,KeyError) as exc:
        # Do not print token contents or authenticated request headers.
        print('Download failed: '+type(exc).__name__+'. Partial bytes are retained for verified resume.')
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
