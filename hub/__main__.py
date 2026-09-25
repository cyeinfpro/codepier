from __future__ import annotations
import argparse
import getpass
import json
from shared.config import ConfigurationError, DEFAULT_HUB_PORT
from hub.config import HubConfig
from shared.instance_lock import InstanceLock
import os
import sqlite3
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from hub.store import Store
from shared.crypto import password_hash
from shared.util import VERSION, fsync_directory


def write_backup(store: Store, output: Path):
    key_lock = InstanceLock(store.directory / ".keys.lock")
    try:
        return _write_backup(store, output)
    finally:
        key_lock.close()


def _write_backup(store: Store, output: Path):
    """Publish a private, completed online backup without overwriting any file."""
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep staging on the output filesystem so exclusive publication is atomic.
    with tempfile.TemporaryDirectory(prefix=".codepier-backup-", dir=output.parent) as temp:
        target = Path(temp) / "hub.sqlite3"
        connection = sqlite3.connect(target)
        try:
            with store.lock:
                store.db.backup(connection)
        finally:
            connection.close()
        archive = Path(temp) / "backup.zip"
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(target, "hub.sqlite3")
                z.write(store.directory / "master.key", "master.key")
                keyring = store.directory / "master.keys.json"
                if keyring.exists() or keyring.is_symlink():
                    from hub.keyring import private_read
                    z.writestr("master.keys.json", private_read(keyring))
            handle.flush()
            os.fsync(handle.fileno())
        # An output appearing during backup must not be overwritten or removed.
        os.link(archive, output)
        fsync_directory(output.parent)


def main():
    parser = argparse.ArgumentParser(description="CodePier Hub")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--data-dir", default=os.getenv("HUB_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="本机创建面板管理员；不会开放网页抢注")
    init.add_argument("--username", default="admin")
    reset = sub.add_parser("reset-password", help="通过服务器本机权限重置账号并撤销全部登录授权")
    reset.add_argument("--username", default="admin")
    run = sub.add_parser("run")
    run.add_argument("--host", default=os.getenv("HUB_HOST", "0.0.0.0"))
    run.add_argument("--port", default=os.getenv("HUB_PORT", str(DEFAULT_HUB_PORT)), type=int)
    backup = sub.add_parser("backup", help="一致性备份数据库和 master.key；备份含敏感凭据")
    backup.add_argument("--output", required=True)
    rekey = sub.add_parser("rekey", help="离线轮换主密钥；保留私密备份，可恢复中断；运行中的 Hub 不允许轮换")
    rekey.add_argument("--resume", action="store_true", help="继续已记录的密钥轮换，不创建新的轮换")
    args = parser.parse_args()
    os.environ["HUB_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    if args.command == "run":
        if not 1 <= args.port <= 65535:
            parser.error("端口范围应为 1–65535")
        os.environ["HUB_PORT"] = str(args.port)
        try:
            HubConfig.from_env()
        except ConfigurationError as exc:
            parser.error(str(exc))
        import uvicorn
        uvicorn.run("hub.app:create_app", factory=True, host=args.host, port=args.port, workers=1,
                    ws_max_size=16 * 1024 * 1024, proxy_headers=True,
                    forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                    access_log=False, timeout_graceful_shutdown=10)
        return
    if args.command == "rekey":
        from hub.keyring import rotate_key
        try:
            print(json.dumps(rotate_key(args.data_dir, resume=args.resume), ensure_ascii=False))
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        return
    store = Store(args.data_dir)
    try:
        if args.command in {"init", "reset-password"}:
            user = store.one("SELECT * FROM users WHERE username=?", (args.username,))
            if args.command == "init" and store.one("SELECT id FROM users LIMIT 1"):
                parser.error("面板已经初始化。重置请使用 reset-password")
            if args.command == "reset-password" and not user:
                parser.error("账号不存在")
            password = os.getenv("CODEPIER_ADMIN_PASSWORD", os.getenv("RD_ADMIN_PASSWORD"))
            if password is None:
                password = getpass.getpass("管理员密码（至少12位）：")
                if password != getpass.getpass("再次输入密码："):
                    parser.error("两次密码不一致")
            if len(password) < 12 or len(password) > 256 or not 1 <= len(args.username) <= 80:
                parser.error("账号1–80字；密码12–256字")
            hashed = password_hash(password)
            with store.lock, store.db:
                # Serialize local CLI initialization with other initializers.
                store.db.execute("BEGIN IMMEDIATE")
                if user:
                    store.db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, user["id"]))
                    store.db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
                    store.db.execute("UPDATE grants SET revoked=1 WHERE user_id=?", (user["id"],))
                else:
                    if store.db.execute("SELECT id FROM users LIMIT 1").fetchone():
                        parser.error("面板已经初始化。重置请使用 reset-password")
                    store.db.execute("INSERT INTO users VALUES (?,?,?,?)", (uuid.uuid4().hex, args.username, hashed, time.time()))
            store.audit("local-cli", "account." + args.command, args.username)
            print(f"账号 {args.username} 已就绪。没有默认密码。")
        elif args.command == "backup":
            output = Path(args.output).expanduser().resolve()
            if output.exists():
                parser.error("输出文件已存在，不覆盖旧备份")
            write_backup(store, output)
            print(f"备份已保存：{output}。包含设备密钥解密材料，请私密保管。")
    finally:
        store.close()

if __name__ == "__main__":
    main()
