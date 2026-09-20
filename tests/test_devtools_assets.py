"""Static distribution and syntax checks for the developer tools workspace."""
from pathlib import Path
from scripts.check_release import check_web_assets
from shared.util import VERSION
from html.parser import HTMLParser
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('path',['web/integration-ui.js','web/integrations.js','web/app.js','web/chat.js','web/workflows.js'])
def test_developer_tools_scripts_parse(path):
    result=subprocess.run(['node','--check',str(ROOT/path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


def test_helpers_are_loaded_before_the_workspace_and_assets_exist():
    class Assets(HTMLParser):
        def __init__(self):
            super().__init__();self.items=[]
        def handle_starttag(self,tag,attrs):
            values=dict(attrs)
            if tag=='script' and 'src' in values:self.items.append(values['src'])
            if tag=='link' and values.get('rel')=='stylesheet':self.items.append(values['href'])
    parser=Assets();parser.feed((ROOT/'web/index.html').read_text())
    files=[item.split('?')[0].replace('/static/','web/') for item in parser.items]
    assert files.index('web/integration-ui.js')<files.index('web/integrations.js')
    assert 'web/integration-flow.css' in files
    for file in files:
        assert (ROOT/file).is_file(),file
    assets = check_web_assets(ROOT, VERSION)
    for file in ['integration-ui.js','integration-flow.css','integrations.js','app.js','chat.js','workflows.js']:
        assert file in assets, file


def test_source_distribution_keeps_new_assets_but_not_private_test_outputs():
    from scripts.build_source_bundle import include
    for file in ['web/integration-ui.js','web/integration-flow.css','docs/DEVTOOLS-FLOW-20260918.md','docs/evidence/devtools-flow-20260918/ACCEPTANCE.md','docs/evidence/devtools-flow-20260918/screenshots/overview-320-dark.png']:
        assert include(Path(file)),file
    for file in ['docs/evidence/devtools-flow-20260918/full/summary.json','docs/evidence/devtools-flow-20260918/full/output.log','docs/evidence/devtools-flow-20260918/profile/config.json','web/font.woff2','.work/devtools-flow-20260918/before.json']:
        assert not include(Path(file)),file
