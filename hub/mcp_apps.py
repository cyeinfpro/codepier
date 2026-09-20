"""Two MCP App resources over existing immutable workspace/review tools."""
from __future__ import annotations
from pathlib import Path
from shared.util import DevError
from shared.mcp_protocol import MIME

RESOURCES={
 'ui://codepier/workspace-v1.html':('workspace-v1.html','CodePier 项目工作区'),
 'ui://codepier/changes-v1.html':('changes-v1.html','CodePier 固定改动审阅'),
}
ROOT=Path(__file__).resolve().parents[1]/'web'/'mcp-apps'


def list_resources():
    return [{'uri':uri,'name':name,'mimeType':MIME} for uri,(_,name) in RESOURCES.items()]


def read_resource(uri,public_url):
    canonical={'ui://relay/workspace-v1.html':'ui://codepier/workspace-v1.html','ui://relay/changes-v1.html':'ui://codepier/changes-v1.html'}.get(uri,uri)
    spec=RESOURCES.get(canonical)
    if not spec:raise DevError('RESOURCE_NOT_FOUND','找不到此组件资源',404)
    path=ROOT/spec[0]
    if not path.is_file() or path.is_symlink():raise DevError('APP_BUILD_REQUIRED','组件资源尚未构建；运行 npm ci 和 npm run build（web/mcp-apps）',503)
    data=path.read_bytes()
    if len(data)>2*1024*1024:raise DevError('APP_RESOURCE_LIMIT','组件资源超过打包上限')
    return {'uri':uri,'mimeType':MIME,'text':data.decode('utf-8'),
      '_meta':{'ui':{'prefersBorder':True,'csp':{'connectDomains':[],'resourceDomains':[],'frameDomains':[]}},
       'openai/widgetDescription':spec[1]+'；查看明确选择的任务、原操作日志、固定改动、验证回执与交付物。只读刷新不执行模型；附件导入须用户主动操作。',
       'openai/widgetPrefersBorder':True,
       'openai/widgetCSP':{'connect_domains':[],'resource_domains':[],'redirect_domains':[public_url()]}}}


def attach(result,name,args,value,public_url):
    if name not in {'open_workspace','show_changes','workflows_get'}:return result
    binding={'kind':'changes' if name=='show_changes' else 'workspace',
             'project':value.get('project_alias','') if name=='workflows_get' else args.get('project',''),
             'workflow_id':value.get('workflow_id','') if name=='workflows_get' else '',
             'workspace_id':args.get('workspace_id',''),'operation_id':value.get('operation_id'),
             'review_ref':value.get('review_ref') or args.get('review_ref',''),'panel_url':public_url()}
    result['_meta']={**result.get('_meta',{}),'com.codepier/binding':binding,'me.infpro.relay/binding':binding}
    return result
