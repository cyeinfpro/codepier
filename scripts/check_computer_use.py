"""Read-only local Codex Computer Use diagnosis. Never sends mouse/keyboard input.

Run from the project root: python -m scripts.check_computer_use
An explicit --app additionally requests that one app's current state. The native
provider may display its own permission UI; this script never approves it.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import time
from agent.computer import NativeClient, discover_provider, validate_computer
from shared.computer_media import normalize_content
from shared.computer_contracts import NATIVE_ACTIONS
from shared.util import DevError, VERSION

async def check(args):
    settings=validate_computer({'plugin_root':args.plugin_root, 'codex_home':args.codex_home, 'call_timeout_seconds':args.timeout})
    installation=discover_provider(settings)
    report={'version':VERSION, 'checked_at':time.time(), 'provider':installation,
            'native_catalog_verified':False, 'screen_read_verified':False,
            'input_actions_performed':False, 'screen_capture_requested':bool(args.app)}
    client=NativeClient(installation,args.timeout)
    try:
        await client.start()
        report.update({'native_catalog_verified':True, 'native_tools':sorted(client.tools),
                       'missing_actions':sorted(NATIVE_ACTIONS-set(client.tools)), 'server_info':client.server_info})
        if args.app:
            data=normalize_content(await client.call('get_app_state',{'app':args.app}))
            report.update({'app':args.app, 'native_is_error':data['native_is_error'], 'images':data['images'],
                           'text_chars':len(data['text']), 'screen_read_verified':not data['native_is_error'] and bool(data['images'])})
            if not report['screen_read_verified']:
                report['next']='Inspect the native Codex permission UI and app state locally. No keyboard/mouse input was sent. Missing pixels are not treated as success.'
        else:
            report['next']='Catalog verified only. Use --app with an explicitly authorized app to check a real screenshot; this may require native permission approval.'
    except DevError as exc:
        report.update({'error_code':exc.code, 'message':exc.message,
                       'next':'Check native Codex Computer Use in its own app, permissions and service state. A timeout alone does not identify the cause. No input was sent or retried.'})
    finally:
        await client.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['native_catalog_verified'] and (not args.app or report['screen_read_verified']) else 2

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app',default='',help='Explicit application name/Bundle ID for a single read-only screenshot probe; omitted = catalog only')
    parser.add_argument('--plugin-root',default='',help='Optional local Computer Use plugin root')
    parser.add_argument('--codex-home',default='',help='Optional local Codex home')
    parser.add_argument('--timeout',type=int,default=45,choices=range(5,91),metavar='5..90')
    args=parser.parse_args()
    if args.app and (len(args.app)>512 or '\x00' in args.app):parser.error('Invalid app name')
    try:return asyncio.run(check(args))
    except (OSError,ValueError) as exc:parser.error(str(exc))

if __name__=='__main__':raise SystemExit(main())
