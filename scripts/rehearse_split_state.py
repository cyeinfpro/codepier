"""Opt-in real Docker rehearsal using owned synthetic resources only.

Run from the trusted source tree as root on Linux after explicit host approval.
A supplied locally built candidate image is never pulled or deleted. No production
volume, container, service, credential or network is inspected or mounted.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from scripts import panel_update_runtime as rt
from scripts.panel_updater import Manager

LABEL = "com.codepier.rehearsal"

class SimulatedCrash(BaseException):
    pass

class Rehearsal:
    def __init__(self, identifier, image):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,30}", identifier):
            raise ValueError("Use a unique 6-31 character lowercase rehearsal id")
        self.identifier, self.image = identifier, image
        self.root = Path("/opt") / ("codepier-state-rehearsal-" + identifier)
        if self.root.is_symlink():
            raise ValueError("Symlink rehearsal root")
        self.manifest_path = self.root / "resources.json"
        if self.manifest_path.exists():
            self.manifest = rt.read_json(self.manifest_path)
            if self.manifest.get("id") != identifier or self.manifest.get("image") != image:
                raise ValueError("Manifest identity mismatch")
        else:
            if self.root.exists():
                raise ValueError("Root already exists without matching manifest")
            self.root.mkdir(mode=0o700)
            self.manifest = {"schema_version": 1, "id": identifier, "image": image,
                             "containers": [], "volumes": [], "results": [], "created": time.time()}
            self.save()

    def save(self):
        rt.atomic_json(self.manifest_path, self.manifest)

    def own(self, kind, name):
        if name not in self.manifest[kind]:
            self.manifest[kind].append(name)
            self.save()

    @staticmethod
    def missing(checked, name):
        error = checked.stderr.lower()
        return checked.returncode != 0 and name.lower() in error and any(
            token in error for token in ("no such object", "no such volume", "no such container"))

    def absent(self, kind, name):
        checked = subprocess.run(["docker", kind, "inspect", name], capture_output=True, text=True, timeout=15)
        if checked.returncode == 0:
            raise ValueError("Refusing to adopt pre-existing rehearsal resource")
        if not self.missing(checked, name):
            raise RuntimeError("Docker resource absence is unverified; no create attempted")

    def execute(self, command, *, cwd=None, timeout=120, log=None):
        command = list(command)
        if command[:3] == ["docker", "volume", "create"]:
            name = command[-1]
            self.absent("volume", name)
            self.own("volumes", name)
            command[-1:-1] = ["--label", LABEL + "=" + self.identifier]
        if command[:2] == ["docker", "run"]:
            name = "cpsr-" + self.identifier + "-helper-" + uuid.uuid4().hex[:8]
            self.absent("container", name)
            self.own("containers", name)
            command[2:2] = ["--name", name, "--label", LABEL + "=" + self.identifier,
                            "--cpus", "1", "--memory", "512m", "--pids-limit", "256"]
            # Every data mount must be one of this manifest's fresh volumes.
            for index, item in enumerate(command):
                if item == "--volume":
                    mount = command[index + 1].split(":", 1)[0]
                    if mount not in self.manifest["volumes"]:
                        raise ValueError("Refusing non-fixture helper volume")
        return rt.run(command, cwd=cwd or self.root, timeout=timeout, log=log)

    def helper(self, volume, program, *, readonly=False):
        if volume not in self.manifest["volumes"]:
            raise ValueError("Unknown synthetic volume")
        return self.execute(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:size=64m,mode=1777",
            "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--volume", volume + ":/app/data" + (":ro" if readonly else ""),
            "--entrypoint", "python", self.image, "-c", program])

    def fixture(self, name):
        folder = self.root / name
        if folder.exists():
            raise ValueError("Scenario already exists; inspect original receipt, do not replay")
        folder.mkdir(mode=0o700)
        home = folder / ".codepier-updater"
        (home / "run").mkdir(parents=True, mode=0o755)
        os.chmod(home / "run", 0o755)
        project = "cpsr-" + self.identifier + "-" + name
        container = project + "-hub"
        self.absent("container", container)
        self.own("containers", container)
        volume = project + "-assets"
        self.execute(["docker", "volume", "create", volume])
        self.helper(volume, """
from pathlib import Path
from hub.store import Store
from shared.native_cli import database
root=Path('/app/data')
s=Store(root)
s.execute("INSERT INTO meta(key,value) VALUES ('rehearsal-proof','synthetic-original')")
s.close()
(root/'fixture-attachment.txt').write_text('synthetic attachment')
(root/'fixture-old-backup.zip').write_bytes(b'synthetic archive')
db=database(root/'native-cli')
db.execute('CREATE TABLE rehearsal_proof(value TEXT)')
db.execute("INSERT INTO rehearsal_proof VALUES ('synthetic-native')")
db.commit();db.close()
""")
        spec = {"name": project, "services": {"hub": {
            "image": self.image, "container_name": container, "user": "10001:10001",
            "network_mode": "none", "cpus": 1, "mem_limit": "512m", "pids_limit": 256,
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "tmpfs": ["/tmp:size=64m,mode=1777"], "labels": {LABEL: self.identifier},
            "environment": {"HUB_DATA_DIR": "/app/data", "HUB_PORT": "8765",
                "HUB_PANEL_UPDATE_SOCKET": "/run/codepier-updater/updater.sock"},
            "volumes": [{"type": "volume", "source": "hub-data", "target": "/app/data"},
                {"type": "bind", "source": str(home / "run"),
                 "target": "/run/codepier-updater", "read_only": True}]}},
            "volumes": {"hub-data": {"external": True, "name": volume}}}
        rt.save_compose(folder / "compose.json", spec)
        rt.atomic_bytes(folder / ".env", b"COMPOSE_FILE=compose.json\n")
        compose = ["docker", "compose", "--project-name", project, "-f", str(folder / "compose.json")]
        self.execute(compose + ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "90", "hub"],
                     cwd=folder, timeout=120)
        actual = rt.load_compose_config(self.execute(compose + ["config", "--format", "json"], cwd=folder))
        version = self.execute(["docker", "exec", container, "python", "-c",
                                "from shared.util import VERSION; print(VERSION)"], cwd=folder)
        rt.atomic_json(home / "config.json", {"project": project, "repository": "cyeinfpro/codepier"})
        rt.atomic_json(home / "current.json", {"compose": actual, "version": version,
            "files": {p: rt.fingerprint(folder / p) for p in (".env", "compose.json")}})
        runtime = rt.DockerRuntime(folder, home, {"project": project, "repository": "cyeinfpro/codepier"},
                                   execute=self.execute)
        manager = Manager(folder, runtime=runtime, start_workers=False)
        body = {"idempotency_key": "rehearsal-" + self.identifier + "-" + name,
                "current_version": version, "actor": "synthetic-rehearsal"}
        accepted = manager.submit("migrate-state", body, host_migration=True)
        job = rt.read_json(home / "jobs" / accepted["operation"]["id"] / "status.json")
        return manager, runtime, job, volume

    def probe(self, volume):
        return json.loads(self.helper(volume, """
import json,sqlite3,hashlib,tempfile,shutil
from pathlib import Path
p=Path('/app/data')
temporary=tempfile.TemporaryDirectory();snapshot=Path(temporary.name)
def open_copy(relative):
    src=p/relative;dst=snapshot/relative;dst.parent.mkdir(parents=True,exist_ok=True)
    for suffix in ('','-wal'):
        original=src.with_name(src.name+suffix)
        if original.exists():
            if original.stat().st_size>16*1024*1024: raise RuntimeError('Synthetic probe size exceeded')
            shutil.copy2(original,dst.with_name(dst.name+suffix))
    db=sqlite3.connect(dst)
    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('Probe integrity')
    return db
db=open_copy('hub.sqlite3')
value=db.execute("SELECT value FROM meta WHERE key='rehearsal-proof'").fetchone()[0]
extra=db.execute("SELECT value FROM meta WHERE key='rehearsal-new-write'").fetchone()
db.close()
native=open_copy('native-cli/native.sqlite3')
n=native.execute('SELECT value FROM rehearsal_proof').fetchone()[0];native.close()
print(json.dumps({'proof':value,'new_write':extra[0] if extra else None,'native':n,
'attachment':(p/'fixture-attachment.txt').exists(),'archive':(p/'fixture-old-backup.zip').exists(),
'key_sha256':hashlib.sha256((p/'master.key').read_bytes()).hexdigest()}))
""", readonly=True))

    def scenario(self, name, fault=None):
        manager, runtime, job, original = self.fixture(name)
        previous = (manager.root / ".env").read_bytes()
        original_atomic, original_clear = rt.atomic_bytes, runtime.clear_gate
        try:
            if fault == "precommit":
                def crash_atomic(path, content, mode=0o600):
                    original_atomic(path, content, mode)
                    if Path(path) == manager.root / ".env":
                        raise SimulatedCrash()
                rt.atomic_bytes = crash_atomic
            elif fault == "postcommit":
                def crash_clear():
                    original_clear()
                    raise SimulatedCrash()
                runtime.clear_gate = crash_clear
            try:
                manager.perform(job)
                if fault:
                    raise AssertionError("Expected injected interruption did not occur")
            except SimulatedCrash:
                pass
        finally:
            rt.atomic_bytes, runtime.clear_gate = original_atomic, original_clear
        if fault == "postcommit":
            self.helper(job["new_volume"], """
from hub.store import Store
s=Store('/app/data');s.execute("INSERT INTO meta(key,value) VALUES ('rehearsal-new-write','acknowledged-new-state')");s.close()
""")
        if fault:
            Manager(manager.root, runtime=runtime, start_workers=False).recover(manual=True)
        result = rt.read_json(manager.home / "jobs" / job["id"] / "status.json")
        old = self.probe(original)
        if old["proof"] != "synthetic-original" or old["new_write"] is not None or not old["attachment"] or not old["archive"]:
            raise AssertionError("Original synthetic state/files changed")
        if fault == "precommit":
            if result["state"] != "rolled_back" or (manager.root / ".env").read_bytes() != previous:
                raise AssertionError("Precommit recovery did not restore original configuration")
        else:
            if result["state"] != "succeeded" or not result.get("commit_decided"):
                raise AssertionError("Migration was not durably committed")
            new = self.probe(job["new_volume"])
            if new["attachment"] or new["archive"] or new["key_sha256"] != old["key_sha256"]:
                raise AssertionError("State-only copy or key identity mismatch")
            if new["native"] != "synthetic-native":
                raise AssertionError("Native database was not retained")
            if fault == "postcommit" and new["new_write"] != "acknowledged-new-state":
                raise AssertionError("Postcommit recovery discarded acknowledged data")
            # Ordinary Compose, without a frozen -f job file, must use committed state.
            effective = rt.load_compose_config(self.execute(
                ["docker", "compose", "config", "--format", "json"], cwd=manager.root))
            if rt.data_volume(effective)[1] != job["new_volume"]:
                raise AssertionError("Ordinary Compose selects stale state")
        summary = {"scenario": name, "state": result["state"], "job": job["id"],
                   "original_preserved": True, "actual_docker": True, "finished": time.time()}
        self.manifest["results"].append(summary); self.save()
        print(json.dumps(summary), flush=True)
        if name == "success":
            self.next_state_copy(manager, runtime)
            self.next_state_copy(manager, runtime, crash=True)
        # Bound concurrency to one Hub; retain its volumes until explicit cleanup.
        container = "cpsr-" + self.identifier + "-" + name + "-hub"
        self.execute(["docker", "stop", "--time", "15", container], timeout=30)
        return summary

    def next_state_copy(self, manager, runtime, *, crash=False):
        """Exercise later split commit/rollback without requesting a Release."""
        current = rt.read_json(manager.home / "current.json")
        previous_env = (manager.root / ".env").read_bytes()
        previous_volume = rt.data_volume(current["compose"])[1]
        job = {"id": uuid.uuid4().hex, "kind": "apply", "state": "running",
               "phase": "building", "from_version": current["version"],
               "target_version": current["version"], "events": []}
        path = manager.home / "jobs" / job["id"]; path.mkdir(mode=0o700)
        rt.atomic_json(path / "current.before.json", current)
        manager.acquire_install_lock(job)
        job.update(runtime.prepare(job, manager.root))
        # This is a storage-cycle fixture, not a same-version Release operation.
        job["image"] = self.image
        target = rt.read_json(path / "target.spec.json")
        target["services"]["hub"]["image"] = self.image
        rt.save_compose(path / "target.json", target)
        manager.save(job)
        rt.atomic_json(manager.home / "latest.json", {"id": job["id"]})
        container = self.execute(rt.compose_command(path / "old.json", manager.config["project"]) + ["ps", "-q", "hub"], cwd=manager.root)
        package = json.loads(self.execute(["docker", "exec", container, "python", "-c", rt.MANIFEST], cwd=manager.root))
        package = {"agent_sha256": package["sha256"], "agent_bytes": package["bytes"]}
        original_atomic = rt.atomic_bytes
        if crash:
            def crash_atomic(destination, content, mode=0o600):
                original_atomic(destination, content, mode)
                if Path(destination) == manager.root / ".env":
                    raise SimulatedCrash()
            rt.atomic_bytes = crash_atomic
        try:
            try:
                manager.cutover(job, package)
                if crash:
                    raise AssertionError("Expected normal-update interruption did not happen")
            except SimulatedCrash:
                pass
        finally:
            rt.atomic_bytes = original_atomic
        if crash:
            Manager(manager.root, runtime=runtime, start_workers=False).recover(manual=True)
            final = rt.read_json(path / "status.json")
            if final["state"] != "rolled_back" or (manager.root / ".env").read_bytes() != previous_env:
                raise AssertionError("Later split commit crash failed to restore previous config")
            expected = previous_volume
        else:
            if job["state"] != "succeeded":
                raise AssertionError("Later split storage cycle failed")
            expected = job["new_volume"]
        effective = rt.load_compose_config(self.execute(["docker", "compose", "config", "--format", "json"], cwd=manager.root))
        if rt.data_volume(effective)[1] != expected:
            raise AssertionError("Ordinary Compose selected stale state after later update")
        actual_id = self.execute(["docker", "compose", "ps", "-q", "hub"], cwd=manager.root)
        actual = json.loads(self.execute(["docker", "inspect", actual_id], cwd=manager.root))[0]
        if not any(m.get("Destination") == "/app/state" and m.get("Name") == expected for m in actual["Mounts"]):
            raise AssertionError("Running Hub uses stale state after later update")
        summary = {"scenario": "subsequent-copy-rollback" if crash else "subsequent-copy",
                   "job": job["id"], "state": "rolled_back" if crash else "succeeded",
                   "ordinary_compose_current": True, "actual_docker": True, "finished": time.time()}
        self.manifest["results"].append(summary); self.save()
        print(json.dumps(summary), flush=True)


    def wal_regression(self):
        """Real read-only source, no SHM, committed Hub/native WAL and hash proof."""
        source = "cpsr-" + self.identifier + "-wal-source"
        target = "cpsr-" + self.identifier + "-wal-target"
        for volume in (source, target):
            self.execute(["docker", "volume", "create", volume])
        self.helper(source, """
import os,sqlite3
from pathlib import Path
from hub.store import Store
from shared.native_cli import database
root=Path('/app/data');s=Store(root);s.close()
db=sqlite3.connect(root/'hub.sqlite3')
db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
db.execute("INSERT INTO meta(key,value) VALUES ('wal-proof','committed-wal')");db.commit()
native=database(root/'native-cli');native.execute('PRAGMA wal_autocheckpoint=0')
native.execute('CREATE TABLE wal_proof(value TEXT)')
native.execute("INSERT INTO wal_proof VALUES ('native-wal')");native.commit()
os._exit(0)
""")
        self.helper(source, """
from pathlib import Path
root=Path('/app/data')
for relative in ('hub.sqlite3','native-cli/native.sqlite3'):
 p=root/relative
 assert p.with_name(p.name+'-wal').stat().st_size>0
 p.with_name(p.name+'-shm').unlink(missing_ok=True)
""")
        hash_program = """
import hashlib,json
from pathlib import Path
root=Path('/app/data')
names=('hub.sqlite3','hub.sqlite3-wal','native-cli/native.sqlite3','native-cli/native.sqlite3-wal','master.key')
assert not (root/'hub.sqlite3-shm').exists()
assert not (root/'native-cli/native.sqlite3-shm').exists()
print(json.dumps({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names},sort_keys=True))
"""
        before = json.loads(self.helper(source, hash_program, readonly=True))
        copied = json.loads(self.execute(["docker", "run", "--rm", "--network", "none", "--read-only",
            "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--volume", source + ":/backup:ro", "--volume", target + ":/app/data",
            "--entrypoint", "python", self.image, "-c", rt.COPY_MIGRATION]))
        after = json.loads(self.helper(source, hash_program, readonly=True))
        if before != after or copied["integrity"] != "ok":
            raise AssertionError("Read-only source changed or copy failed")
        self.helper(target, """
import sqlite3
from pathlib import Path
root=Path('/app/data')
db=sqlite3.connect((root/'hub.sqlite3').as_uri()+'?mode=ro',uri=True)
assert db.execute("SELECT value FROM meta WHERE key='wal-proof'").fetchone()[0]=='committed-wal'
db.close()
native=sqlite3.connect((root/'native-cli/native.sqlite3').as_uri()+'?mode=ro',uri=True)
assert native.execute('SELECT value FROM wal_proof').fetchone()[0]=='native-wal'
native.close()
assert (root/'.codepier-state.json').exists()
""", readonly=True)
        result = {"scenario": "readonly-wal-copy", "state": "succeeded", "actual_docker": True,
                  "source_hashes_unchanged": True, "hub_wal_replayed": True, "native_wal_replayed": True,
                  "source_shm_absent": True, "finished": time.time()}
        self.manifest["results"].append(result);self.save()
        print(json.dumps(result), flush=True)

    def cleanup(self):
        outcomes = []
        for kind, names in (("container", self.manifest["containers"]), ("volume", self.manifest["volumes"])):
            for name in reversed(names):
                command = ["docker", "inspect", name] if kind == "container" else ["docker", "volume", "inspect", name]
                checked = subprocess.run(command, capture_output=True, text=True, timeout=15)
                if checked.returncode:
                    if not self.missing(checked, name):
                        raise RuntimeError("Docker cleanup state unknown; not reported absent")
                    outcomes.append({"kind": kind, "name": name, "state": "absent"}); continue
                value = json.loads(checked.stdout)[0]
                labels = value.get("Config", {}).get("Labels", {}) if kind == "container" else value.get("Labels", {})
                if labels.get(LABEL) != self.identifier:
                    raise RuntimeError("Cleanup ownership label mismatch; nothing else removed")
                removal = ["docker", "rm", "-f", name] if kind == "container" else ["docker", "volume", "rm", name]
                self.execute(removal, timeout=30)
                outcomes.append({"kind": kind, "name": name, "state": "removed"})
        self.manifest["cleanup"] = outcomes; self.save()
        print(json.dumps({"cleanup_verified": True, "objects": len(outcomes), "evidence": str(self.manifest_path)}))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Approved isolated host rehearsal requires root")
    rehearsal = Rehearsal(args.id, args.image)
    if args.cleanup:
        rehearsal.cleanup(); return
    if rehearsal.manifest["results"] or rehearsal.manifest["volumes"]:
        raise RuntimeError("Existing run must be inspected, not replayed")
    # Local image lookup only; never registry authentication or pulls.
    rehearsal.execute(["docker", "image", "inspect", args.image], timeout=15)
    for name, fault in (("success", None), ("precommit", "precommit"), ("postcommit", "postcommit")):
        rehearsal.scenario(name, fault)
    rehearsal.wal_regression()
    print(json.dumps({"passed": True, "scenarios": 6, "manifest": str(rehearsal.manifest_path)}))

if __name__ == "__main__":
    main()
