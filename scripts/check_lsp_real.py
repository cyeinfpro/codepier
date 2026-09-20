#!/usr/bin/env python3
"""Exercise a preinstalled real Pyright over the complete isolated Hub/Agent path."""
import argparse,json,os,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tests.support import running_stack
from tests.test_integrations_stack import resolved
from shared.util import atomic_json

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--server',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    binary=args.server.resolve(strict=True);checks=[]
    with tempfile.TemporaryDirectory(prefix='codepier-real-lsp-') as tmp:
        with running_stack(Path(tmp)/'stack') as s:
            s.stop_agent();s.config['mcp_policy']={'block_local_codex':True}
            s.config['integrations']={'language_servers':{'python':{'command':[str(binary),'--stdio'],'projects':['Imago'],'timeout_seconds':30,'settings':{'python':{'analysis':{'diagnosticMode':'workspace','autoSearchPaths':True}}}}}}
            atomic_json(s.config_path,s.config);s.start_agent()
            (s.imago/'library.py').write_text('def decorate(name: str) -> str:\n    return "Hello " + name\n\ndef greeting(name: str) -> str:\n    return decorate(name)\n')
            (s.imago/'main.py').write_text('from library import greeting\ndef run() -> str:\n    return greeting("world")\n\ntext: str = run()\nwrong: int = "intentional type error"\n')
            for action,path,line,column in [('symbols','library.py',1,5),('definition','main.py',3,13),('references','library.py',4,6),('hover','main.py',5,2),('diagnostics','main.py',1,1),('workspace_symbols','library.py',1,5),('incoming_calls','library.py',4,6),('outgoing_calls','library.py',4,6)]:
                result=resolved(s,'lsp_query',{'action':action,'language':'python','path':'' if action=='workspace_symbols' else path,'line':line,'column':column,'query':'greeting','limit':100})
                if action=='references':assert {'library.py','main.py'}<=set(i['path'] for i in result['items']),result
                elif action=='incoming_calls':assert any(i['path']=='main.py' for i in result['items']),result
                elif action=='outgoing_calls':assert any(i.get('name')=='decorate' for i in result['items']),result
                elif action=='hover':assert 'str' in result['text'],result
                elif action=='definition':assert any(i['path']=='library.py' for i in result['items']),result
                elif action in {'symbols','references','diagnostics','workspace_symbols','incoming_calls','outgoing_calls'}:assert result['items'],result
                checks.append({'action':action,'passed':True,'items':len(result.get('items',[])),'source_current':result.get('source_current'),'omitted':result.get('omitted'),'paths':[i.get('path') for i in result.get('items',[])],'operation_id':result['operation_id']})
                print(json.dumps(checks[-1]),flush=True)
            assert s.agent.poll() is None
    report={'provider':'Microsoft Pyright','model_turns_started':0,'production_changed':False,'checks':checks,'passed':True}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n');return 0
if __name__=='__main__':raise SystemExit(main())
