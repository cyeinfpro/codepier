"""One process per state directory; avoids duplicate Agent execution and Hub splits."""
from __future__ import annotations
import os
from pathlib import Path

class InstanceLock:
    def __init__(self,path:Path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.file=path.open('a+b')
        try:
            os.chmod(path,0o600)
            if os.name=='nt':
                import msvcrt
                self.file.seek(0);self.file.write(b'0');self.file.flush();self.file.seek(0)
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,PermissionError) as exc:
            self.file.close()
            raise RuntimeError(f'此状态目录已有实例运行，不能重复启动：{path.parent}') from exc
    def close(self):
        if not self.file.closed:
            try:
                if os.name=='nt':
                    import msvcrt
                    self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
            finally:
                self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
