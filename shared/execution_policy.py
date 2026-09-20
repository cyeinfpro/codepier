"""Static invocation guard, NOT a sandbox or a proof about arbitrary program behavior.

Only inspect executable positions, nested shell commands and recognized launch APIs.
Never run an executable to identify it. Hub uses text-only inspection; Agent can also
resolve symlinks and inspect explicitly invoked local scripts. See docs/MCP_EXECUTION_POLICY.md.
"""
from __future__ import annotations

import ast
import os
import re
import shlex
from pathlib import Path

from shared.util import DevError

POLICY_VERSION = 1
DENIAL_MESSAGE = "MCP 不允许启动本地 Codex；普通命令、文件读写和技能读取不受此规则限制，手动使用请在管理员面板操作"
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_POWERSHELL = {"powershell", "pwsh"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def hub_blocks_codex() -> bool:
    # Retain the existing deployment switch; local Agent policy is an independent floor.
    return os.getenv("MCP_BLOCK_LOCAL_CODEX", "").strip().lower() in {"1", "true", "yes", "on"}


def validate_policy(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"block_local_codex"}:
        raise ValueError("mcp_policy 只接受 block_local_codex 配置")
    result = {"block_local_codex": False, **value}
    if type(result["block_local_codex"]) is not bool:
        raise ValueError("mcp_policy.block_local_codex 必须是 true/false")
    return result


def agent_blocks_codex(config: dict, project: dict) -> bool:
    # This metadata is injected by Hub AFTER authentication, outside tool arguments.
    policy = project.get("_execution_policy")
    local = config.get("mcp_policy", {}).get("block_local_codex", False)
    if policy is None:
        return local  # Backward compatibility; local=true also protects legacy Hubs.
    if (not isinstance(policy, dict) or type(policy.get("version")) is not int
            or policy.get("version") != POLICY_VERSION
            or not isinstance(policy.get("origin"), str) or policy["origin"] not in {"panel", "mcp"}
            or type(policy.get("block_local_codex")) is not bool):
        return True  # Malformed metadata never grants the panel exemption.
    if policy["origin"] == "panel":
        return False
    return local or policy["block_local_codex"]


def computer_denial(name: str, args: dict) -> bool:
    from shared.computer_contracts import COMPUTER_TOOLS
    return (name == "computer_status" and bool(args.get("probe"))) or (
        name in COMPUTER_TOOLS - {"computer_status", "computer_session_close"})


def _base(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def _program(value: str) -> str:
    return re.sub(r"\.(exe|cmd|bat|ps1)$", "", _base(value))


def _codex(value: str) -> bool:
    value = value.replace("\\", "/").lower()
    return bool(re.search(r"(?:^|/)@openai/codex(?:-sdk)?(?:@[^/]+)?(?:/|$)", value)
                or _program(value) == "codex"
                or re.fullmatch(r"codex-(?:aarch64|x86_64|arm64)[\w.-]*", _base(value)))


def _python(name: str) -> bool:
    return bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|py", name))


class InvocationInspector:
    """Bounded best-effort static analysis; unknown dynamic code is not executed."""
    def __init__(self, *, cwd: Path | None = None, env: dict | None = None):
        self.cwd = Path(cwd) if cwd is not None else None
        self.env = dict(env or {})
        self.files: set[tuple[Path, str | None]] = set()
        self.remaining = 20000

    def _check_budget(self, depth: int) -> None:
        if depth > 24 or self.remaining <= 0:
            raise DevError("EXECUTION_POLICY_LIMIT", "命令结构超过静态检查上限；请拆成较小的命令，未启动进程", 403)

    def _resolve(self, value: str, *, search_path: bool = True) -> Path | None:
        if self.cwd is None or not value or any(x in value for x in ("$", "`", "\x00")):
            return None
        path = Path(value).expanduser()
        candidates = [path if path.is_absolute() else self.cwd / path]
        if search_path and "/" not in value and "\\" not in value:
            candidates = [(Path(p) if Path(p).is_absolute() else self.cwd / p) / value
                          for p in self.env.get("PATH", os.defpath).split(os.pathsep)]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
        return None

    def _script(self, value: str, depth: int, *, search_path: bool = False, language: str | None = None) -> bool:
        path = self._resolve(value, search_path=search_path)
        self._check_budget(depth)
        if path is None or (path, language) in self.files or len(self.files) >= 16:
            return False
        if _codex(str(path)):
            return True
        # Do not load arbitrary executables, dependencies, directories or huge files.
        try:
            if path.stat().st_size > 262144:
                return False
            with path.open("rb") as stream:
                raw = stream.read(262145)
            if len(raw) > 262144 or b"\x00" in raw:
                return False
            source = raw.decode("utf-8")
        except (OSError, UnicodeError):
            return False
        self.files.add((path, language))
        suffix = path.suffix.lower()
        first = source.split("\n", 1)[0] if source.startswith("#!") else ""
        if language == "python" or language is None and (suffix == ".py" or "python" in first):
            return self.python(source, depth + 1)
        if language == "javascript" or language is None and (suffix in {".js", ".mjs", ".cjs"} or "node" in first):
            return self.javascript(source, depth + 1)
        if language == "shell" or language is None and (suffix in {".sh", ".bash", ".zsh", ".ps1", ".cmd", ".bat"} or any(x in first for x in ("/sh", "bash", "zsh"))):
            return self.shell(source, depth + 1)
        return False

    def _interpreter(self, name: str, arguments: list[str], depth: int) -> bool:
        """Interpreter flags end at the script filename; following words are data.

        Explicit interpreters determine a file's language even without a suffix
        or shebang. This is bounded inspection, not a full option parser.
        """
        language = "python" if _python(name) else "javascript" if name in {"node", "nodejs", "deno"} else "shell"
        i, stdin_script = 0, False
        while i < len(arguments):
            arg = arguments[i]
            lower = arg.lower()
            if arg == "--":
                i += 1
                break
            if arg == "-":
                return False  # Input streams are not inferred from unrelated arguments.
            if language == "python" and arg == "-m":
                return False  # Module/dependency execution is outside file inspection.
            inline = (language == "python" and arg == "-c" or
                      language == "javascript" and arg in {"-e", "--eval", "-p", "--print"} or
                      language == "shell" and (lower in {"-command", "/c", "/k"} or
                                                re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", arg)))
            if inline:
                if i + 1 >= len(arguments):
                    return False
                source = arguments[i + 1]
                return (self.python(source, depth + 1) if language == "python" else
                        self.javascript(source, depth + 1) if language == "javascript" else
                        self.shell(source, depth + 1))
            if name in _POWERSHELL and lower in {"-file", "-f"}:
                i += 1
                break
            if language == "shell" and re.fullmatch(r"-[A-Za-z]*s[A-Za-z]*", arg):
                stdin_script = True
            value_options = ({"-W", "-X"} if language == "python" else
                             {"-r", "--require", "--import", "--loader", "--conditions"} if language == "javascript" else
                             {"-o", "-O", "--rcfile", "--init-file"})
            if arg in value_options:
                if (language == "javascript" and arg in {"-r", "--require", "--import", "--loader"}
                        and i + 1 < len(arguments) and self._script(arguments[i + 1], depth + 1, language="javascript")):
                    return True
                i += 2
            elif arg.startswith("-"):
                i += 1
            else:
                break
        return not stdin_script and i < len(arguments) and self._script(arguments[i], depth + 1, language=language)

    def argv(self, argv: list[str], depth: int = 0) -> bool:
        self._check_budget(depth)
        if not argv:
            return False
        self.remaining -= 1
        command, rest = argv[0], argv[1:]
        name = _program(command)
        if _codex(command):
            return True
        resolved = self._resolve(command)
        if resolved is not None and _codex(str(resolved)):
            return True
        if name == "command" and rest and rest[0] in {"-v", "-V", "-pv", "-pV"}:
            return False  # Lookup, not execution.
        if name in {"env", "sudo", "doas", "nice", "nohup", "time", "command", "exec", "builtin", "xargs"}:
            i = 0
            environment = dict(self.env)
            # The same flag can be unary for one wrapper and value-taking for
            # another: sudo -n is NOT nice -n or xargs -n.
            values = {
                "env": {"-u", "--unset", "-C", "--chdir"},
                "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-T", "--command-timeout", "-D", "--chdir", "-R", "--chroot", "-r", "--role", "-t", "--type"},
                "doas": {"-u", "-C"},
                "nice": {"-n", "--adjustment"},
                "time": {"-f", "--format", "-o", "--output"},
                "exec": {"-a"},
                "xargs": {"-I", "--replace", "-L", "--max-lines", "-P", "--max-procs", "-d", "--delimiter", "-s", "--max-chars", "-a", "--arg-file", "-n", "--max-args", "-E", "--eof"},
            }.get(name, set())
            while i < len(rest):
                arg = rest[i]
                if name == "env" and arg in {"-S", "--split-string"} and i + 1 < len(rest):
                    original = self.env
                    self.env = environment
                    try:
                        return self.shell(rest[i + 1], depth + 1)
                    finally:
                        self.env = original
                if name == "env" and arg in {"-i", "--ignore-environment", "-"}:
                    environment.clear()
                    i += 1
                elif name == "env" and arg in {"-u", "--unset"} and i + 1 < len(rest):
                    environment.pop(rest[i + 1], None)
                    i += 2
                elif arg in values:
                    i += 2
                elif arg == "--":
                    i += 1
                    break
                elif _ASSIGNMENT.match(arg):
                    key, value = arg.split("=", 1)
                    environment[key] = value
                    i += 1
                elif arg.startswith("-"):
                    i += 1
                else:
                    break
            original = self.env
            self.env = environment
            try:
                return self.argv(rest[i:], depth + 1)
            finally:
                self.env = original
        if name in _SHELLS | _POWERSHELL | {"cmd"}:
            return self._interpreter(name, rest, depth + 1)
        if name in {"eval", "invoke-expression", "iex"}:
            return self.shell(" ".join(rest), depth + 1)
        if name in {"source", "."}:
            return bool(rest) and self._script(rest[0], depth + 1, language="shell")
        if name == "find":
            return any(self.argv(rest[i + 1:], depth + 1) for i, x in enumerate(rest) if x in {"-exec", "-execdir", "-ok", "-okdir"})
        if name in {"npx", "npm", "pnpm", "yarn", "bun", "bunx"}:
            # Inspect package selectors and the executable, not arbitrary data
            # arguments (e.g. `npx echo codex` must remain a plain echo).
            if name not in {"npx", "bunx"}:
                if not rest or rest[0] not in {"exec", "dlx", "x"}:
                    return False
                rest = rest[1:]
            i = 0
            while i < len(rest):
                arg = rest[i]
                if arg in {"-c", "--call"} and i + 1 < len(rest):
                    return self.shell(rest[i + 1], depth + 1)
                if arg in {"-p", "--package"} and i + 1 < len(rest):
                    if _codex(rest[i + 1]):
                        return True
                    i += 2
                elif arg.startswith("--package="):
                    if _codex(arg.partition("=")[2]):
                        return True
                    i += 1
                elif arg == "--":
                    return self.argv(rest[i + 1:], depth + 1)
                elif arg.startswith("-"):
                    i += 1
                else:
                    return self.argv(rest[i:], depth + 1)
            return False
        if name == "start-process":
            for i, arg in enumerate(rest):
                if arg.lower() == "-filepath" and i + 1 < len(rest):
                    return _codex(rest[i + 1])
            return bool(rest) and _codex(rest[0])
        if name == "start":
            return bool(rest) and _codex(rest[0])
        if name == "open":
            return any(x in {"-a", "-b"} and i + 1 < len(rest) and (_program(rest[i + 1]) in {"codex", "codex.app"} or rest[i + 1] == "com.openai.codex") for i, x in enumerate(rest))
        if _python(name) or name in {"node", "nodejs", "deno"}:
            return self._interpreter(name, rest, depth + 1)
        return self._script(command, depth + 1, search_path=True)

    @staticmethod
    def _word(node, variables: dict[str, str]) -> str:
        text = node.text.decode("utf-8", "replace")
        kind = node.type
        if kind == "raw_string":
            return text[1:-1]
        if kind in {"simple_expansion", "expansion"}:
            key = text[2:-1] if text.startswith("${") else text[1:]
            return variables.get(key, text)
        if kind in {"command_name", "concatenation", "string"}:
            return "".join(InvocationInspector._word(c, variables) for c in node.named_children)
        if kind == "ansi_c_string":
            try:
                return bytes(text[2:-1], "utf-8").decode("unicode_escape")
            except UnicodeError:
                return text
        if kind == "word":
            return re.sub(r"\\(.)", r"\1", text)
        return text

    def shell(self, source: str, depth: int = 0, *, aliases: dict | None = None) -> bool:
        original = self.env
        self.env = dict(original)
        try:
            return self._shell(source, depth, aliases=aliases)
        finally:
            self.env = original

    def _shell(self, source: str, depth: int, *, aliases: dict | None = None) -> bool:
        self._check_budget(depth)
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_bash
        except ImportError as exc:
            raise DevError("EXECUTION_POLICY_UNAVAILABLE", "执行策略缺少 tree-sitter-bash；请更新 Hub/Agent 依赖，文件工具仍可用", 503) from exc
        parser = Parser(Language(tree_sitter_bash.language()))
        root = parser.parse(source.encode("utf-8")).root_node
        variables = dict(self.env)
        aliases = dict(aliases or {})
        stack = [root]
        while stack:
            self._check_budget(depth)
            node = stack.pop()
            self.remaining -= 1
            if node.type == "variable_assignment":
                key, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                if key is not None and value is not None and node.parent.type != "command":
                    name = key.text.decode()
                    variables[name] = self._word(value, variables)
                    if name in self.env or (node.parent.type == "declaration_command" and node.parent.text.lstrip().startswith(b"export ")):
                        self.env[name] = variables[name]
            if node.type == "command":
                command = node.child_by_field_name("name")
                if command is not None:
                    args = [self._word(command, variables)] + [self._word(c, variables) for c in node.children_by_field_name("argument")]
                    if args[0] == "alias":
                        for item in args[1:]:
                            if _ASSIGNMENT.match(item):
                                key, value = item.split("=", 1)
                                aliases[key] = value
                    elif args[0] == "unalias":
                        for key in args[1:]:
                            aliases.pop(key, None)
                    elif args[0] in aliases and self.shell(aliases[args[0]] + " " + shlex.join(args[1:]), depth + 1, aliases=aliases):
                        return True
                    environment = dict(self.env)
                    for assignment in node.named_children:
                        if assignment.type == "variable_assignment":
                            key, value = assignment.child_by_field_name("name"), assignment.child_by_field_name("value")
                            if key is not None and value is not None:
                                environment[key.text.decode()] = self._word(value, variables)
                    original = self.env
                    self.env = environment
                    try:
                        if self.argv(args, depth + 1):
                            return True
                    finally:
                        self.env = original
                    parent = node.parent
                    if parent is not None and parent.type == "redirected_statement":
                        for redirect in parent.named_children:
                            if redirect.type != "heredoc_redirect":
                                continue
                            body = next((c for c in redirect.named_children if c.type == "heredoc_body"), None)
                            if body is not None:
                                text = body.text.decode("utf-8", "replace")
                                name = _program(args[0])
                                if name in _SHELLS and self.shell(text, depth + 1):
                                    return True
                                if _python(name) and self.python(text, depth + 1):
                                    return True
                                if name in {"node", "nodejs"} and self.javascript(text, depth + 1):
                                    return True
            stack.extend(reversed(node.named_children))
        return False

    def python(self, source: str, depth: int = 0) -> bool:
        self._check_budget(depth)
        if len(source) > 262144:
            return False
        try:
            root = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            return False
        symbols: dict[str, object] = {}
        aliases = {"subprocess": "subprocess", "os": "os"}
        def literal(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return symbols.get(node.id)
            if isinstance(node, (ast.List, ast.Tuple)):
                return [literal(x) for x in node.elts]
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                a, b = literal(node.left), literal(node.right)
                if isinstance(a, str) and isinstance(b, str):
                    return (a + b)[:262144]
            return None
        for node in ast.walk(root):
            self._check_budget(depth)
            self.remaining -= 1
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name] = item.name
            elif isinstance(node, ast.ImportFrom):
                for item in node.names:
                    aliases[item.asname or item.name] = (node.module or "") + "." + item.name
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols[target.id] = literal(node.value)
            elif isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                head, dot, tail = function.partition(".")
                function = aliases.get(head, head) + (dot + tail if dot else "")
                if function in {"eval", "exec"} and node.args:
                    code = literal(node.args[0])
                    if isinstance(code, str) and self.python(code, depth + 1):
                        return True
                supported = function in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput", "os.system", "os.popen"} or function.startswith(("os.exec", "os.spawn"))
                if not supported:
                    continue
                target = node.args[0] if node.args else next((x.value for x in node.keywords if x.arg in {"args", "command"}), None)
                value = literal(target)
                if isinstance(value, str) and self.shell(value, depth + 1):
                    return True
                if isinstance(value, list) and value and all(isinstance(x, str) for x in value) and self.argv(value, depth + 1):
                    return True
        return False

    def javascript(self, source: str, depth: int = 0) -> bool:
        self._check_budget(depth)
        from tree_sitter import Language, Parser
        import tree_sitter_javascript
        root = Parser(Language(tree_sitter_javascript.language())).parse(source.encode()).root_node
        modules, launchers = set(), {}
        supported = {"exec", "execSync", "execFile", "execFileSync", "spawn", "spawnSync"}
        def text(node):
            return node.text.decode() if node is not None else ""
        def literal(node):
            if node is None or node.type != "string":
                return None
            try:
                value = ast.literal_eval(text(node))
                return value if isinstance(value, str) else None
            except (ValueError, SyntaxError):
                return None
        def required(node):
            if node is None or node.type != "call_expression" or text(node.child_by_field_name("function")) != "require":
                return None
            args = node.child_by_field_name("arguments")
            return literal(args.named_children[0]) if args is not None and args.named_children else None
        stack = [root]
        while stack:
            self._check_budget(depth)
            node = stack.pop()
            self.remaining -= 1
            if node.type == "variable_declarator" and required(node.child_by_field_name("value")) in {"child_process", "node:child_process"}:
                binding = node.child_by_field_name("name")
                if binding is not None and binding.type == "identifier":
                    modules.add(text(binding))
                elif binding is not None and binding.type == "object_pattern":
                    for item in binding.named_children:
                        if item.type == "pair_pattern":
                            launchers[text(item.child_by_field_name("value"))] = text(item.child_by_field_name("key"))
                        else:
                            launchers[text(item)] = text(item)
            if node.type == "import_statement":
                module = literal(node.child_by_field_name("source"))
                if module and _codex(module):
                    return True
                if module in {"child_process", "node:child_process"}:
                    imports = list(node.named_children)
                    while imports:
                        item = imports.pop()
                        if item.type == "import_specifier":
                            name = item.child_by_field_name("name")
                            alias = item.child_by_field_name("alias")
                            launchers[text(alias or name)] = text(name)
                        elif item.type == "identifier":
                            modules.add(text(item))
                        else:
                            imports.extend(item.named_children)
            if node.type == "call_expression":
                module = required(node)
                if module and _codex(module):
                    return True
                function, arguments = node.child_by_field_name("function"), node.child_by_field_name("arguments")
                method = launchers.get(text(function))
                if function is not None and function.type == "member_expression":
                    owner = function.child_by_field_name("object")
                    if text(owner) in modules or required(owner) in {"child_process", "node:child_process"}:
                        method = text(function.child_by_field_name("property"))
                # RegExp.exec(), unrelated objects and strings are not process APIs.
                if method in supported and arguments is not None and arguments.named_children:
                    args = arguments.named_children
                    value = literal(args[0])
                    if value is not None:
                        if method in {"exec", "execSync"} and self.shell(value, depth + 1):
                            return True
                        argv = [value]
                        if len(args) > 1 and args[1].type == "array":
                            tail = [literal(c) for c in args[1].named_children]
                            if all(isinstance(c, str) for c in tail):
                                argv.extend(tail)
                        if method not in {"exec", "execSync"} and self.argv(argv, depth + 1):
                            return True
            stack.extend(reversed(node.named_children))
        return False


def enforce_argv(config: dict, project: dict, command: list[str], cwd: Path, env: dict) -> None:
    if agent_blocks_codex(config, project) and InvocationInspector(cwd=cwd, env=env).argv(command):
        raise DevError("CODEX_REMOTE_DISABLED", DENIAL_MESSAGE, 403)
