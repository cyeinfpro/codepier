"""Exercise installer validation without downloading, changing services or running Docker."""
import os, shutil, subprocess
from pathlib import Path
import pytest

@pytest.mark.parametrize('host,port,valid',[
    ('192.0.2.20','8765',True),('codepier.example.com','08080',True),('[2001:db8::1]','8765',True),
    ('bad host','8765',False),('x/y','8765',False),('192.0.2.20','0',False),('192.0.2.20','65536',False)
])
def test_installer_validates_ip_port_without_running_docker(tmp_path,host,port,valid):
    base=Path(__file__).resolve().parent.parent
    (tmp_path/'deploy').mkdir();(tmp_path/'bin').mkdir()
    shutil.copy(base/'deploy/install-hub.sh',tmp_path/'deploy/install-hub.sh')
    shutil.copy(base/'.env.example',tmp_path/'.env.example')
    fake=tmp_path/'bin/docker';fake.write_text('#!/usr/bin/env bash\n[[ "$*" == "compose version" || "$*" == "compose config --quiet" ]] && exit 0\n[[ "$*" == "compose build hub" ]] && exit 77\nexit 99\n');fake.chmod(0o700)
    result=subprocess.run(['bash',str(tmp_path/'deploy/install-hub.sh')],input=f'{host}\n{port}\n',text=True,capture_output=True,env={**os.environ,'PATH':str(tmp_path/'bin')+':'+os.environ['PATH']},timeout=3)
    assert result.returncode==(77 if valid else 1),result.stdout+result.stderr
    if valid:
        env=(tmp_path/'.env').read_text(); assert f'HUB_PUBLIC_URL=http://{host}:{int(port)}' in env
