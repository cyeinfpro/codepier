"""Read-only interoperability probe using the official MCP SDK, not CodePier's client."""
from __future__ import annotations
import argparse
import asyncio
import json
from importlib.metadata import version
import os
import stat
from pathlib import Path
import httpx2
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def read_token(path):
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(path,flags)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>2048 or os.name!='nt' and info.st_mode&0o077:
            raise ValueError('Use a private, small regular token file')
        return os.read(fd,2049).decode('utf-8').strip()
    finally:os.close(fd)


async def probe(url,token_file,project,mode='legacy'):
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+read_token(token_file)},timeout=20,follow_redirects=False,trust_env=False) as http:
        transport=streamable_http_client(url,http_client=http,terminate_on_close=False)
        async with Client(transport,mode=mode,read_timeout_seconds=20) as client:
            catalog=await client.list_tools()
            schemas={tool.name:tool.model_dump(by_alias=True)['outputSchema'] for tool in catalog.tools}
            for schema in schemas.values():Draft202012Validator.check_schema(schema)
            checks=[]
            async def invoke(name,args):
                result=await client.call_tool(name,args)
                value=result.model_dump(by_alias=True,exclude_none=True)
                Draft202012Validator(schemas[name]).validate(value['structuredContent'])
                checks.append({'tool':name,'is_error':value.get('isError',False),'schema_valid':True})
                return value
            resolved=await invoke('projects_resolve',{'project':project})
            assert not resolved.get('isError')
            diagnosis=await invoke('diagnostics_get',{'project':project})
            assert diagnosis['structuredContent']['client_catalog']['matches'] is None
            read=await invoke('fs_read',{'project':project,'path':'README.md','max_lines':20})
            data=read['structuredContent']
            if data.get('pending'):
                for _ in range(5):
                    receipt=await invoke('operations_wait',{'operation_id':data['operation_id'],'wait_seconds':2})
                    if not receipt['structuredContent']['pending']:
                        assert receipt['structuredContent']['state']=='succeeded';break
                else:raise AssertionError('Read did not complete')
            else:assert 'content' in data
            error=await invoke('fs_read',{'project':project,'path':'.env'})
            assert error.get('isError') and error['structuredContent']['error']['code']=='PROTECTED_PATH'
            await invoke('artifacts_list',{'project':project})
            resources=await client.list_resources();prompts=await client.list_prompts()
            assert resources.resources and prompts.prompts
            if client.protocol_version in {'2025-03-26','2025-06-18','2025-11-25'}:
                await client.send_ping()
            else:
                assert client.protocol_version=='2026-07-28', 'Unverified MCP revision'
            return {'sdk':'official mcp '+version('mcp'),'mode':mode,'protocol_version':client.protocol_version,
                    'server':client.server_info.model_dump(by_alias=True),'tool_count':len(schemas),'checks':checks,
                    'resources':len(resources.resources),'prompts':len(prompts.prompts),
                    'scope':'Read-only real HTTP interoperability; not certification of every optional MCP extension.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True);parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--project',required=True);parser.add_argument('--mode',choices=['legacy','auto'],default='legacy')
    args=parser.parse_args()
    result=asyncio.run(probe(args.url,args.token_file,args.project,args.mode))
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
