"""Fixed review resource and inert compatibility for retired workspace cards."""
from __future__ import annotations
from pathlib import Path
from shared.util import DevError
from shared.mcp_protocol import MIME

RESOURCES = {'ui://codepier/changes-v1.html': ('changes-v1.html', 'CodePier 固定改动审阅')}
RETIRED_WORKSPACE = 'ui://codepier/workspace-v1.html'
# Saved host resources remain readable, but are never advertised as new UI.
LEGACY_RESOURCES = {
    RETIRED_WORKSPACE: RETIRED_WORKSPACE,
    'ui://relay/workspace-v1.html': RETIRED_WORKSPACE,
    'ui://relay/changes-v1.html': 'ui://codepier/changes-v1.html',
}
ROOT = Path(__file__).resolve().parents[1] / 'web' / 'mcp-apps'
RETIREMENT_HTML = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>CodePier</title><body><p>项目与任务看板已移除。</p>'
    '<p>请直接在聊天中使用 project_query 查询项目、task_query 查询原操作。'
    '核心开发工具、项目上下文、附件和网页管理面板仍可使用。</p></body></html>')


def list_resources():
    return [{'uri': uri, 'name': name, 'mimeType': MIME} for uri, (_, name) in RESOURCES.items()]


def read_resource(uri, public_url):
    canonical = LEGACY_RESOURCES.get(uri, uri)
    if canonical == RETIRED_WORKSPACE:
        return {'uri': uri, 'mimeType': MIME, 'text': RETIREMENT_HTML,
                '_meta': {'ui': {'csp': {'connectDomains': [], 'resourceDomains': [], 'frameDomains': []}}}}
    spec = RESOURCES.get(canonical)
    if not spec:
        raise DevError('RESOURCE_NOT_FOUND', '找不到此组件资源', 404)
    path = ROOT / spec[0]
    if not path.is_file() or path.is_symlink():
        raise DevError('APP_BUILD_REQUIRED', '组件资源尚未构建；运行 npm ci 和 npm run build（web/mcp-apps）', 503)
    data = path.read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise DevError('APP_RESOURCE_LIMIT', '组件资源超过打包上限')
    return {'uri': uri, 'mimeType': MIME, 'text': data.decode('utf-8'),
        '_meta': {'ui': {'prefersBorder': True, 'csp': {'connectDomains': [], 'resourceDomains': [], 'frameDomains': []}},
            'openai/widgetDescription': spec[1] + '；按固定快照读取差异，不重新采样或执行命令。',
            'openai/widgetPrefersBorder': True,
            'openai/widgetCSP': {'connect_domains': [], 'resource_domains': [], 'redirect_domains': [public_url()]}}}


def attach(result, name, args, value, public_url):
    if name == 'read' and args.get('operation') == 'changes':
        name = 'show_changes'
        args = {**args, **args.get('options', {})}
    if name != 'show_changes':
        return result
    binding = {'kind': 'changes', 'project': args.get('project', ''),
        'workspace_id': args.get('workspace_id', ''), 'operation_id': value.get('operation_id'),
        'review_ref': value.get('review_ref') or args.get('review_ref', ''), 'panel_url': public_url()}
    result['_meta'] = {**result.get('_meta', {}), 'com.codepier/binding': binding, 'me.infpro.relay/binding': binding}
    return result
