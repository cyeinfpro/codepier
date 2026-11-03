"""Short isolated reads must not initialize unrelated desktop model schemas."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def child(code, *args):
    completed = subprocess.run([sys.executable, '-c', code, *map(str, args)], cwd=ROOT,
                               capture_output=True, text=True, timeout=10, check=True)
    return json.loads(completed.stdout)


def test_importing_file_primitives_does_not_initialize_desktop_schema():
    loaded = child('''import json, sys
from agent.core_files import CoreFiles
print(json.dumps({'media': 'shared.computer_media' in sys.modules,
                  'contracts': 'shared.computer_contracts' in sys.modules,
                  'pydantic': 'pydantic' in sys.modules}))
''')
    assert loaded == {'media': False, 'contracts': False, 'pydantic': False}


def test_cold_text_read_keeps_real_file_and_sha_guards_without_desktop_import(tmp_path):
    (tmp_path / 'project').mkdir()
    (tmp_path / 'project' / 'readme.txt').write_text('第一行\nsecond line\n', encoding='utf-8')
    result = child('''import json, sys
from pathlib import Path
from types import SimpleNamespace
from agent.core_files import CoreFiles
from agent.filesystem import FileEngine
from shared.util import DevError
base = Path(sys.argv[1])
root = base / 'project'
engine = FileEngine({'allowed_roots': [{'path': str(root), 'writable': False}]},
                    SimpleNamespace(directory=base / 'journal'), base / 'config.json')
files = CoreFiles(engine)
project = {'root': str(root), 'alias': 'Fixture', 'mode': 'read'}
args = {'path': 'readme.txt', 'offset': 1, 'limit': 1, 'expected_sha256': None}
page = files.call('read', project, args)
second = files.call('read', project, {**args, 'offset': 2, 'expected_sha256': page['sha256']})
errors = []
for change in ({'expected_sha256': '0' * 64}, {'path': '../outside'}, {'path': '.env'}):
    try:
        files.call('read', project, {**args, **change})
    except DevError as exc:
        errors.append(exc.code)
print(json.dumps({'first': page['content'], 'next_offset': page['next_offset'],
                  'second': second['content'], 'sha_match': page['sha256'] == second['sha256'],
                  'errors': errors, 'media': 'shared.computer_media' in sys.modules,
                  'contracts': 'shared.computer_contracts' in sys.modules,
                  'pydantic': 'pydantic' in sys.modules}))
''', tmp_path)
    assert result == {'first': '第一行\n', 'next_offset': 2, 'second': 'second line\n',
                      'sha_match': True, 'errors': ['SHA_CONFLICT', 'INVALID_PATH', 'PROTECTED_PATH'],
                      'media': False, 'contracts': False, 'pydantic': False}


@pytest.mark.parametrize('mime,encoded', [
    ('image/png', 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=='),
    ('image/jpeg', '/9j/wAALCAABAAMBAREA/9k='),
])
def test_image_normalization_loads_contracts_only_at_mcp_boundary(mime, encoded):
    result = child('''import json, sys
from shared.computer_media import normalize_content, mcp_result
normalized = normalize_content({'content': [{'type': 'image', 'mimeType': sys.argv[1], 'data': sys.argv[2]}]})
before = 'shared.computer_contracts' in sys.modules
rendered = mcp_result('read', normalized)
print(json.dumps({'contracts_before': before, 'contracts_after': 'shared.computer_contracts' in sys.modules,
                  'mime': rendered['content'][1]['mimeType'], 'data': rendered['content'][1]['data'],
                  'width': rendered['structuredContent']['images'][0]['width'],
                  'height': rendered['structuredContent']['images'][0]['height'],
                  'private_content_removed': '_computer_content' not in rendered['structuredContent'],
                  'is_error': rendered['isError']}))
''', mime, encoded)
    assert not result['contracts_before'] and result['contracts_after']
    assert result['mime'] == mime and result['data'] == encoded
    assert result['width'] == (1 if mime == 'image/png' else 3) and result['height'] == 1
    assert result['private_content_removed'] and not result['is_error']
