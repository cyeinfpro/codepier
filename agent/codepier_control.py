#!/usr/bin/env python3
"""Owner-local control client. Never prints credentials or restarts a service."""
import argparse,json,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from agent.codepier_browser_host import post

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--descriptor',required=True)
    parser.add_argument('action',choices=['status','pause','resume','stop']);parser.add_argument('--project');parser.add_argument('--confirm')
    parser.add_argument('--include-native',action='store_true');parser.add_argument('--idempotency-key')
    args=parser.parse_args()
    if args.action!='status' and (not args.project or args.confirm!=args.project):parser.error('Explicit --project and matching --confirm are required')
    key=args.idempotency_key or uuid.uuid4().hex
    try:
        if args.action=='status':result=post(args.descriptor,'/status',{})
        else:
            print('operation recovery key: '+key,file=sys.stderr)
            result=post(args.descriptor,'/control',{'project':args.project,'confirm':args.confirm,'action':args.action,'include_native':args.include_native,'idempotency_key':key})
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result.get('ok',True) else 1
    except (ValueError,OSError) as exc:
        print(json.dumps({'error':str(exc),'retry_same_key':key,'effects_confirmed':False},ensure_ascii=False),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
