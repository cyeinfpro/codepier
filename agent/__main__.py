from __future__ import annotations
import argparse
import asyncio
from contextlib import nullcontext
import json
import os
import signal
import sys
from pathlib import Path
from shared.brand_migration import default_base
from shared.util import atomic_json, VERSION
from agent.config import validate_config

DEFAULT = Path.home() / ".codepier-agent" / "config.json"


def main():
    # Redirected Windows service logs can use a legacy code page. Logging a
    # localized status line must never tear down an authenticated connection.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser(description="CodePier 家用 Agent — 只主动连接，不开放家里端口")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", default=None, help="配置路径（放在子命令之前）")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="导入面板下载的配对文件")
    init.add_argument("--pairing-file")
    init.add_argument("--re-pair", action="store_true", help="仅更新同一设备的配对凭据，保留目录、任务与能力配置")
    init.add_argument("--allow", action="append", help="可多次指定本机授权目录")
    init.add_argument("--hub")
    init.add_argument("--shell", choices=("full", "disabled"), help="新安装默认开启当前用户 Shell 和目录任务；disabled 同时关闭新目录任务。重新配对保持原权限")
    run_parser = sub.add_parser("run", help="连接并保持运行（前台诊断）")
    run_parser.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    config = sub.add_parser("configure", help="修改地址、端口或目录授权；运行中的 Agent 会自动重连")
    config.add_argument("--hub")
    config.add_argument("--allow", action="append", help="替换授权目录列表")
    config.add_argument("--shell", choices=("full", "disabled"), help="本机开启/关闭当前用户完整 Shell 权限；full 授权所有允许执行的项目")
    config.add_argument("--codex-skills", choices=("enabled", "disabled"), help="本机授权只读接入 Codex 技能，不授予脚本执行权限")
    config.add_argument("--skill-project", action="append", help="替换 Codex 技能可见项目 ID/别名列表；可重复，* 表示所有已授权项目")
    config.add_argument("--codex-home", help="可选 Codex 主目录；仅读取 skills/ 与技能禁用设置")
    config.add_argument("--skill-root", action="append", help="增加本机技能集合或单技能目录，需指定 --skill-project")
    config.add_argument("--computer", choices=("enabled", "disabled"), help="本机开启/关闭桌面控制，独立于 Shell 和 Skills")
    config.add_argument("--computer-project", action="append", help="替换桌面控制允许的项目 ID/别名，可重复")
    config.add_argument("--computer-app", action="append", help="替换允许的应用名称/Bundle ID；* 代表明确授权全部应用")
    config.add_argument("--computer-plugin-root", help="可选，本机安装的 Computer Use 插件绝对路径")
    config.add_argument("--computer-codex-home", help="可选，Computer Use 使用的本机 CODEX_HOME")
    sub.add_parser("computer-stop", help="本机停止桌面控制，不停止开发 Agent")
    sub.add_parser("computer-resume", help="本机解除桌面紧急停止，仍需配置和原生权限")
    sub.add_parser("show", help="显示配置摘要，不显示密钥")
    args = parser.parse_args()
    from shared.brand_migration import default_base
    path = (Path(args.config).expanduser() if args.config else default_base()/'config.json').resolve()
    if args.command == "init":
        if path.exists() and not args.re_pair:
            parser.error(f"配置已存在：{path}；修改地址请使用 configure，重新配对请使用 init --re-pair --pairing-file，保留原目录授权和任务配置")
        pairing = Path(args.pairing_file or input("面板下载的配对 JSON 文件路径：").strip().strip('"')).expanduser()
        try:
            c = json.loads(pairing.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            parser.error(f"无法读取配对文件：{exc}")
        required = {"device_id", "secret", "hub_url"}
        if not isinstance(c, dict) or not required.issubset(c):
            parser.error("配对文件缺少 device_id / secret / hub_url")
        if args.re_pair:
            try:
                if args.allow is not None or args.shell is not None:
                    raise ValueError('--re-pair 不接受 --allow 或 --shell；原目录授权和执行能力不会改变')
                before=path.read_bytes();existing=json.loads(before)
                validate_config(existing,path)
                if existing.get('device_id')!=c['device_id']:
                    raise ValueError('配对文件不属于此设备；原配置未修改')
                replacement={**existing,'secret':c['secret'],'hub_url':args.hub or c['hub_url']}
                validate_config(replacement,path)
                from datetime import datetime,timezone
                backup=path.with_name(path.name+'.before-repair-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
                with backup.open('xb') as stream:
                    os.chmod(backup,0o600);stream.write(before);stream.flush();os.fsync(stream.fileno())
                if path.read_bytes()!=before:
                    raise ValueError('配置在核对期间已改变；未覆盖更新，请重新核对')
                atomic_json(path,replacement)
            except (OSError,ValueError,RecursionError) as exc:
                parser.error(str(exc))
            print('同一设备已重新配对；目录授权、任务、能力配置与历史路径均已保留。请按原维护流程重启 Agent。')
            return
        c = {k: c[k] for k in ("device_id", "secret", "hub_url", "name") if k in c}
        c["hub_url"] = args.hub or c["hub_url"]
        roots = args.allow or [input("授权项目的父目录（例如 D:\\Projects 或 /home/me/projects）：").strip().strip('"')]
        if not all(x and Path(x).expanduser().is_dir() for x in roots):
            parser.error("每个授权目录必须已经存在于这台电脑")
        execution = args.shell != "disabled"
        c["allowed_roots"] = [{"path": str(Path(x).expanduser().resolve()), "writable": True, "allow_tasks": execution} for x in roots]
        c["shell"] = {"enabled": execution, "projects": ["*"] if execution else []}
        c["state_dir"] = str(path.parent / "state")
        c["tasks"] = {}
        try:
            c = validate_config(c, path)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        atomic_json(path, c)
        print("本机 Shell / 目录任务：" + ("已开启。Shell 使用当前系统用户权限，不是目录沙箱；仍需面板项目与客户端执行授权。" if execution else "已关闭。"))
        print(f"已保存：{path}\n连接地址：{c['hub_url']}\n启动：python -m agent --config \"{path}\" run\n配对文件含设备密钥，请妥善保管或删除。")
        if os.name == "nt":
            print("Windows 请通过文件属性/NTFS ACL 限制配置目录权限，仅允许当前用户读取。")
    elif args.command in {"computer-stop", "computer-resume"}:
        try:
            c = validate_config(json.loads(path.read_text(encoding="utf-8")), path)
            stop = Path(c["state_dir"]) / "computer-use.stopped"
            if args.command == "computer-stop":
                atomic_json(stop, {"stopped": True})
                print("本机桌面控制已禁用；Agent 会停止 CodePier 后续输入。不撤销已发生的动作。")
            else:
                stop.unlink(missing_ok=True)
                print("已解除本机紧急停止；新会话仍需本机配置、computer 授权与原生权限。")
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    elif args.command in {"configure", "show"}:
        try:
            c = validate_config(json.loads(path.read_text(encoding="utf-8")), path)
        except (OSError, ValueError, RecursionError) as exc:
            parser.error(f"无法读取有效配置：{exc}")
        if args.command == "configure":
            c["hub_url"] = args.hub or (c["hub_url"] if args.shell or args.allow is not None or args.codex_skills or args.skill_project is not None or args.codex_home or args.skill_root or args.computer or args.computer_project is not None or args.computer_app is not None or args.computer_plugin_root or args.computer_codex_home else input(f"面板地址/IP:端口 [{c['hub_url']}]：").strip() or c["hub_url"])
            if args.allow is not None:
                if not all(Path(x).expanduser().is_dir() for x in args.allow):
                    parser.error("授权目录必须存在")
                c["allowed_roots"] = [{"path": str(Path(x).expanduser().resolve()), "writable": True, "allow_tasks": False} for x in args.allow]
            if args.shell:
                c["shell"]["enabled"] = args.shell == "full"
                if args.shell == "full":
                    c["shell"]["projects"] = ["*"]
                    for root in c["allowed_roots"]:
                        if root.get("writable", True):
                            root["allow_tasks"] = True
            if args.computer:
                c["computer"]["enabled"] = args.computer == "enabled"
            if args.computer_project is not None:
                c["computer"]["projects"] = args.computer_project
            if args.computer_app is not None:
                c["computer"]["allowed_apps"] = args.computer_app
            if args.computer_plugin_root:
                c["computer"]["plugin_root"] = args.computer_plugin_root
            if args.computer_codex_home:
                c["computer"]["codex_home"] = args.computer_codex_home
            if args.codex_skills:
                c["skills"]["codex_enabled"] = args.codex_skills == "enabled"
            if args.skill_project is not None:
                c["skills"]["codex_projects"] = args.skill_project
            if args.codex_home:
                c["skills"]["codex_home"] = args.codex_home
            if args.skill_root:
                selected = args.skill_project or c["skills"]["codex_projects"]
                if not selected:
                    parser.error("增加 --skill-root 必须通过 --skill-project 显式选择项目")
                for supplied in args.skill_root:
                    directory = Path(supplied).expanduser()
                    if not directory.is_absolute() or not directory.is_dir():
                        parser.error("--skill-root 必须是已存在的绝对目录")
                    item = {"path": str(directory.resolve()), "projects": list(selected)}
                    c["skills"]["extra_roots"] = [r for r in c["skills"]["extra_roots"] if r["path"] != item["path"]] + [item]
            if c["skills"]["codex_enabled"] and not c["skills"]["codex_projects"]:
                parser.error("启用 Codex 技能时请通过 --skill-project 显式选择可见项目")
            try:
                c = validate_config(c, path)
            except (ValueError, OSError) as exc:
                parser.error(str(exc))
            atomic_json(path, c)
            print("配置已保存。运行中的 Agent 将自动加载并重新连接；迁移面板时需要保留云端数据与设备密钥。")
        public = {k: v for k, v in c.items() if k != "secret"}
        # Local task environment values can contain credentials; show names only.
        public["tasks"] = {name: {**task, **({"env": {k: "<redacted>" for k in task["env"]}} if "env" in task else {})} for name, task in public.get("tasks", {}).items()}
        if "shell" in public:
            public["shell"] = {**public["shell"], "env": {k: "<redacted>" for k in public["shell"].get("env", {})}}
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        from agent.runner import Agent
        async def run():
            from agent.service_watchdog import watch_event_loop
            with watch_event_loop() if args.supervised else nullcontext():
                agent = Agent(path)
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        loop.add_signal_handler(sig, lambda: asyncio.create_task(agent.stop()))
                    except (NotImplementedError, RuntimeError):
                        pass
                await agent.run()
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
