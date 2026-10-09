"""Execute production rendering/binding without a browser; visual journeys are separate."""
import json
import subprocess
from pathlib import Path

from tests.test_audit_api import api as api

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    isConnected:true, hidden:false, innerHTML:'', textContent:'', elements:{},
    addEventListener(){}, setAttribute(){}, replaceChildren(){}, focus(){},
    scrollIntoView(){}, classList:{add(){},remove(){}},
  });
  return elements.get(id);
}
global.window=global;
global.addEventListener=()=>{};
global.S={session:{user_id:'fixture'},space_id:'legacy',page:'settings'};
global.$=(selector)=>element(selector);
global.$$=()=>[];
global.esc=(value)=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
global.heading=(title)=>'<h1>'+title+'</h1>';
global.CodePierAppearance={getPreference:()=> 'auto'};
global.CodePierPanelUpdate={html:()=>'<section id="panel-update"></section>',bind(){}};
global.sessionChanged=()=>new Error('stale');
global.api=async path=>path.startsWith('/api/settings/catalog')?input.catalog:input.ingress;
global.toast=()=>{};
vm.runInThisContext(fs.readFileSync('web/settings-file-import.js','utf8'));
vm.runInThisContext(fs.readFileSync('web/settings.js','utf8'));
CodePierSettings.html().then(html=>{
  CodePierSettings.bind();
  process.stdout.write(JSON.stringify({html,dirty:CodePierSettings.dirty()}));
}).catch(error=>{console.error(error);process.exitCode=1;});
"""


def render(client):
    catalog = client.get("/api/settings/catalog")
    assert catalog.status_code == 200, catalog.text
    ingress = client.get("/api/settings/file-import")
    result = subprocess.run(["node", "-e", SCRIPT], cwd=ROOT,
        input=json.dumps({"catalog": catalog.json(), "ingress": ingress.json()}),
        text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_production_render_and_bind_keep_inline_and_safe_flows_separate(api):
    _, client, _ = api
    result = render(client)
    assert result["dirty"] is False
    html = result["html"]
    for identifier in ("settings-center", "settings-form", "access-settings-form", "access-batch-form",
                       "file-import-settings-form", "settings-project", "panel-update"):
        assert 'id="' + identifier + '"' in html
    assert 'id="password-form"' not in html
    assert 'data-settings-entry="identity"' in html
    assert "预览修改" in html and "单独确认并批量应用" in html


def test_non_admin_render_has_personal_controls_and_no_instance_editor(api):
    app, client, _ = api
    app.state.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    result = render(client)
    assert 'id="access-settings-form"' in result["html"]
    assert 'id="settings-form"' not in result["html"]
    assert 'id="file-import-settings-form"' not in result["html"]
    assert str(app.state.store.directory) not in result["html"]
