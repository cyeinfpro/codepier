#!/usr/bin/env python3
"""Build the independent native host on the target OS with installed PyInstaller.
No packages are installed, no browser registration/configuration is changed.
"""
import argparse,hashlib,importlib.util,json,subprocess,sys,tempfile
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);args=parser.parse_args()
    if importlib.util.find_spec('PyInstaller') is None:parser.error('Install PyInstaller in a separate build environment first; no automatic installation was performed.')
    target=Path(args.output).expanduser().absolute();name='codepier-browser-host.exe' if sys.platform=='win32' else 'codepier-browser-host'
    if (target/name).exists():parser.error('Output already exists; choose a new build directory.')
    target.mkdir(parents=True,exist_ok=True)
    source=Path(__file__).resolve().parents[1]/'agent/codepier_browser_host.py'
    with tempfile.TemporaryDirectory(prefix='codepier-browser-build-') as temporary:
        command=[sys.executable,'-m','PyInstaller','--onefile','--console','--clean','--name','codepier-browser-host','--distpath',str(target),'--workpath',str(Path(temporary)/'work'),'--specpath',temporary,str(source)]
        subprocess.run(command,check=True)
    artifact=target/name
    if not artifact.is_file():raise RuntimeError('Native executable was not produced')
    metadata={'file':name,'bytes':artifact.stat().st_size,'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),'platform':sys.platform,'python':sys.version,'account_flow_verified':False}
    (target/'manifest.json').write_text(json.dumps(metadata,indent=2));print(json.dumps(metadata));return 0
if __name__=='__main__':raise SystemExit(main())
