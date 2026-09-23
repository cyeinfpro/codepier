#!/usr/bin/env python3
"""Build a source-only CodePier ZIP without credentials, dependencies or databases.

The archive preserves executable modes and includes a per-file SHA256 manifest.
It is not an Agent state backup. Run from any cwd; only this source tree is read.
"""
from __future__ import annotations
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {'agent','hub','shared','web','scripts','deploy','skills','tests','docs','.github'}
ROOT_FILES = {'codepier','codepier.ps1','install.sh','.dockerignore','.gitignore','.env.example','Dockerfile','compose.yml','LICENSE',
              'README.md','CHANGELOG.md','LOCAL_RELEASE.json','RELEASE.json',
              'SECURITY.md','CONTRIBUTING.md','.gitattributes','ruff.toml',
              'requirements.txt','requirements-agent.txt','requirements-bridge.txt',
              'requirements-dev.txt','requirements-compat.txt','requirements-tools.txt'}
EXTENSIONS = {'.py','.js','.mjs','.cjs','.ts','.tsx','.html','.css','.json','.toml','.yaml','.yml',
              '.md','.txt','.sh','.ps1','.cmd','.bat','.svg','.png','.jpg','.jpeg','.webp','.ico',
              '.xml','.service','.plist','.example'}
EXCLUDED_NAMES = {'.codepier-updater','__pycache__','node_modules','.git','.pytest_cache','.venv','.venv-compat',
                  'data','private','agent-state','hub-data','uploads','backups','checkpoints','.DS_Store',
                  '.work','dist','.ruff_cache','.mypy_cache','.idea','.vscode'}
PRIVATE_NAMES = {'auth.json','config.json','pairing.json','master.key','.env','credentials.json'}
INTEGRATION_EVIDENCE_FILES = {'verification.json','feature-matrix.json','source-changes.json',
    'full-regression.xml','targeted.xml','real-lsp.json','browser-final.xml','apps-final.xml',
    'distribution-check.json'}

EVIDENCE_FILES = {'full-regression-final.xml','focused-verified.xml','worker-real-results.json',
                  'windows-controller-contract.xml','native-finish-focused.xml','boot-recovery-fixed.xml',
                  'browser-final.xml','ACCEPTANCE_LOCAL_1.8.0.md','COMPLETION_2026-09-14.md',
                  'desktop-terminal.png','mobile-native-hardened.png','artifact-bundle.json'}


REQUIRED_FILES = {'hub/panel_update.py','shared/panel_maintenance.py','web/panel-update.js','web/panel-update.css','scripts/panel_updater.py','scripts/panel_update_runtime.py','scripts/panel_update_source.py','codepier','codepier.ps1','web/brand.js','shared/brand_migration.py','shared/brand_browser.py','agent/brand_upgrade.py','scripts/migrate_hub.py','scripts/migrate_hub_data.py','scripts/migrate_hub_networks.py','scripts/migrate_hub_proxy.py','scripts/rename_checkout.py','install.sh','deploy/install-hub.sh','scripts/install_agent.py','scripts/agent_lifecycle.py',
                'deploy/install-from-hub.sh','deploy/install-from-hub.ps1','agent/native_worker.py','agent/native_windows.py','agent/native_pi_bridge.py',
                'agent/runner.py','hub/app.py','hub/native_cli.py','shared/native_cli.py',
                'agent/chat_worker.py','agent/chat_catalog.py','agent/claude_cli.py','agent/claude_protocol.py',
                'web/index.html','web/chat.js','web/chat.css','web/chat-chrome.js',
                'web/chat-history.js','web/chat-catalog.js',
                'web/chat-markdown.js','web/chat-panels.js','web/native-hash.js',
                'requirements-agent.txt',
                'agent/integrations.py','agent/incoming_artifacts.py','agent/lsp_navigation.py',
                'agent/background_browser.py','agent/integration_local.py','agent/source_versions.py',
                'agent/workspaces.py','agent/integration_state.py','agent/integration_control.py',
                'agent/setup_integrations.py','agent/install_browser_bridge.py',
                'agent/codepier_browser_host.py','agent/codepier_control.py',
                'hub/integrations.py','hub/mcp_apps.py','hub/workspace_status.py','shared/mcp_protocol.py',
                'web/mcp-apps/app.js','web/mcp-apps/app.css','web/mcp-apps/dashboard.js',
                'web/mcp-apps/review.js','web/mcp-apps/workspace-tools.js','web/mcp-apps/ui.js',
                'docs/MCP-WORKSPACE-DASHBOARD-20260917.md',
                'shared/integration_contracts.py','web/integrations.js','web/integrations.css',
                'web/integration-ui.js','web/integration-flow.css','docs/DEVTOOLS-FLOW-20260918.md',
                'web/browser-extension.zip','web/integration-assets.json','web/integration-guide.md',
                'web/mcp-apps/workspace-v1.html','web/mcp-apps/changes-v1.html',
                'web/mcp-apps/THIRD_PARTY_NOTICES.txt','docs/INTEGRATIONS-20260917.md'}

PUBLIC_DOCS = {
    'docs/PANEL_UPDATE.md', 'docs/CLAUDE_CLI.md',
    'docs/INTEGRATIONS-20260917.md', 'docs/MCP-WORKSPACE-DASHBOARD-20260917.md',
    'docs/DEVTOOLS-FLOW-20260918.md', 'docs/RELEASING.md', 'docs/VPS.md', 'docs/ARCHITECTURE.md', 'docs/LONG_OPERATIONS.md',
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def include(relative, *, public=False):
    if relative.is_absolute() or '..' in relative.parts:
        return False
    if public and (relative.name == 'LOCAL_RELEASE.json' or
                   relative.parts[:1] == ('docs',) and relative.as_posix() not in PUBLIC_DOCS):
        return False
    if relative.as_posix() == 'web/browser-extension.zip':
        return True
    parts = relative.parts
    if any(part in EXCLUDED_NAMES or part.startswith('._') for part in parts):
        return False
    name = relative.name.lower()
    if name in PRIVATE_NAMES or any(fnmatch.fnmatchcase(name, pattern) for pattern in
        ('token*.txt', 'pairing*.json', '*.sqlite3*', '*.sqlite-*', '*.db-*', '*.key', '*.pem')):
        return False
    if name.startswith('.env') and name != '.env.example':
        return False
    # Never include installed font files, any credential key, or runtime stores.
    if relative.suffix.lower() in {'.woff','.woff2','.ttf','.otf','.ttc','.pem','.key','.p12','.pfx',
                                  '.sqlite','.sqlite3','.db','.log','.zip','.pyc'}:
        return False
    if len(parts) == 1:
        return relative.name in ROOT_FILES
    if parts[0] not in DIRECTORIES:
        return False
    if parts[:2] == ('docs','evidence'):
        # Only reviewed, credential-free closeout evidence is distributable.
        # Never recursively include logs, descriptors, profiles or backup archives.
        if len(parts) >= 4 and parts[2] == 'codepier-rename-20260918':
            return (len(parts) == 4 and relative.name in {'ACCEPTANCE.md','verification.json','name-audit.json','delivery.json','macos-services.json','docker-migration.json'} or
                    len(parts) == 5 and parts[3] == 'screenshots' and relative.suffix=='.png')
        if len(parts) >= 4 and parts[2] == 'devtools-flow-20260918':
            return (len(parts) == 4 and relative.name in {'ACCEPTANCE.md','verification.json','controls-coverage.md','delivery.json'} or
                    len(parts) == 5 and parts[3] == 'screenshots' and relative.name in {'overview-1440-light.png','overview-320-dark.png','validation-1440-light.png','navigation-390-light.png','setup-390-light.png'})
        if len(parts) >= 4 and parts[2] == 'cli-global-flow-20260918':
            return (len(parts) == 4 and relative.name in {'ACCEPTANCE.md','verification.json','controls-coverage.md','source-delivery.json'} or
                    len(parts) == 5 and parts[3] == 'screenshots' and relative.name in {'conversation-light-1440.png','conversation-dark-390.png','history-dark-390.png','project-light-1440.png','project-dark-390.png','manifest.json'})
        if len(parts) >= 4 and parts[2] == 'workspace-dashboard-20260917':
            return (len(parts) == 4 and relative.name in {'ACCEPTANCE.md', 'verification-summary.json', 'source-comparison.json'} or
                    len(parts) == 5 and parts[3] == 'screenshots' and relative.name in {'dashboard-1100-light.png', 'dashboard-390-dark.png', 'apps-review-1100.png', 'apps-review-390.png'})
        if len(parts) == 4 and parts[2] == 'integration-closeout-20260917':
            return relative.name in INTEGRATION_EVIDENCE_FILES
        return (len(parts) >= 4 and parts[2] == 'native-cli-20260914' and
                (relative.name in EVIDENCE_FILES or
                 len(parts) == 5 and parts[3] == 'official-sdk-final' and relative.suffix == '.json'))
    return relative.suffix.lower() in EXTENSIONS or relative.name in {'LICENSE','Dockerfile','Caddyfile'}


def build(destination, *, public=False):
    destination = destination.resolve()
    if ROOT not in destination.parents or destination.suffix.lower() != '.zip' or destination.relative_to(ROOT).parts[0] not in {'dist', '.work'}:
        raise ValueError('Bundle destination must be a ZIP inside dist/ or .work/, never a runtime source asset')
    destination.parent.mkdir(parents=True, exist_ok=True)
    files, skipped, total = [], [], 0
    for root, dirs, names in os.walk(ROOT, followlinks=False):
        current = Path(root)
        kept = []
        for name in sorted(dirs):
            if name in EXCLUDED_NAMES or name.startswith('.venv'):
                continue
            if current == ROOT and name not in DIRECTORIES:
                continue
            relative_directory = (current / name).relative_to(ROOT).as_posix()
            if public and relative_directory.startswith('docs/') and not any(
                document.startswith(relative_directory + '/') for document in PUBLIC_DOCS):
                continue
            if (current / name).is_symlink():
                raise RuntimeError('Selected source directory is a symlink: ' + relative_directory)
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            source = current/name
            relative = source.relative_to(ROOT)
            if not include(relative, public=public):
                continue
            before = source.lstat()
            if not stat.S_ISREG(before.st_mode) or source.is_symlink():
                raise RuntimeError('Selected source is not a regular file: '+relative.as_posix())
            if before.st_size > 8*1024*1024:
                raise RuntimeError('Selected source exceeds 8 MiB: '+relative.as_posix())
            with source.open('rb') as stream:
                pinned = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise RuntimeError('Selected source was replaced: '+relative.as_posix())
                raw = stream.read(8*1024*1024+1)
                after = os.fstat(stream.fileno())
            if (before.st_mtime_ns,before.st_size,before.st_ino)!=(after.st_mtime_ns,after.st_size,after.st_ino):
                raise RuntimeError('Source changed during packaging: '+relative.as_posix())
            total += len(raw)
            if total > 128*1024*1024:
                raise RuntimeError('Source bundle exceeds 128 MiB uncompressed')
            files.append((relative.as_posix(),raw,before.st_mode & 0o777))
    required = REQUIRED_FILES - ({'LOCAL_RELEASE.json'} if public else set())
    missing = required - {name for name,_,_ in files}
    if missing:
        raise RuntimeError('Bundle is missing runtime files: '+', '.join(sorted(missing)))
    inventory = {name:{'sha256':digest(raw),'bytes':len(raw),'mode':oct(mode)} for name,raw,mode in files}
    manifest = ''.join(item['sha256']+'  '+name+'\n' for name,item in sorted(inventory.items())).encode()
    fd, temporary = tempfile.mkstemp(prefix='.codepier-source-',suffix='.zip',dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=7) as archive:
            for name,raw,mode in files:
                info=zipfile.ZipInfo(name,date_time=(2026,9,14,0,0,0))
                info.create_system=3;info.external_attr=(stat.S_IFREG|mode)<<16
                info.compress_type=zipfile.ZIP_DEFLATED
                archive.writestr(info,raw)
            info=zipfile.ZipInfo('MANIFEST.sha256',date_time=(2026,9,14,0,0,0))
            info.create_system=3;info.external_attr=(stat.S_IFREG|0o644)<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,manifest)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('ZIP CRC verification failed')
            for name,item in inventory.items():
                if digest(archive.read(name)) != item['sha256']:
                    raise RuntimeError('ZIP content SHA mismatch: '+name)
        os.replace(temporary,destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result={'path':str(destination.relative_to(ROOT)),'bytes':destination.stat().st_size,
            'sha256':digest(destination.read_bytes()),'files':len(files)+1,'source_bytes':total,
            'verified':True,'source_only':True,'public_profile':public,'excluded':'credentials, runtime DBs/state, environments, caches, old operation logs, font files, previous archives',
            'skipped_selected_files':skipped,'inventory':inventory}
    metadata=destination.with_suffix('.manifest.json')
    metadata.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {key:value for key,value in result.items() if key!='inventory'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist/codepier-1.9.1-source.zip')
    parser.add_argument('--public',action='store_true',help='Omit local release records and historical evidence; use this profile for GitHub')
    args=parser.parse_args()
    print(json.dumps(build(args.output,public=args.public),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
