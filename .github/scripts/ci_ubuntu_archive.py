"""Select the official HTTPS Ubuntu archive on disposable GitHub Linux runners.

Only the exact Azure Ubuntu archive URI changes. No trust settings, suites,
components, security archive, third-party sources or APT budgets are changed.
"""
import os
from pathlib import Path
import re
import stat
import tempfile

OLD = {"http://azure.archive.ubuntu.com/ubuntu", "https://azure.archive.ubuntu.com/ubuntu"}
NEW = "https://archive.ubuntu.com/ubuntu"


def uri(value):
    if value.removesuffix("/") in OLD:
        return NEW + ("/" if value.endswith("/") else "")
    if "azure.archive.ubuntu.com" in value:
        raise ValueError("Unexpected Azure archive URI; refusing to rewrite")
    return value


def transform(text, deb822):
    if not deb822:
        output = []
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith("#") or "azure.archive.ubuntu.com" not in line:
                output.append(line)
                continue
            match = re.match(r"^(\s*deb(?:-src)?\s+(?:\[[^\]\n]+\]\s+)?)(\S+)(\s+)(\S+)(.*)$", line.rstrip("\r\n"))
            if not match:
                raise ValueError("Malformed Azure legacy source")
            prefix, address, space, suite, rest = match.groups()
            if not rest.strip() or rest.lstrip().startswith("#"):
                raise ValueError("Missing legacy source components")
            replacement = uri(address)
            if suite.endswith("-security"):
                output.append(line)
            else:
                ending = line[len(line.rstrip("\r\n")):]
                output.append(prefix + replacement + space + suite + rest + ending)
        return "".join(output)
    # Keep every byte outside URI tokens, including comments and signing keys.
    blocks = re.split(r"(\r?\n[ \t]*\r?\n)", text)
    for i in range(0, len(blocks), 2):
        block = blocks[i]
        fields = {}
        current = None
        for line in block.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0].isspace():
                if current is None:
                    raise ValueError("Orphan deb822 continuation")
                fields[current] += " " + line.strip()
            else:
                match = re.match(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]*(.*)$", line)
                if not match:
                    raise ValueError("Malformed deb822 source")
                current = match[1].lower()
                if current in fields:
                    raise ValueError("Duplicate deb822 field")
                fields[current] = match[2]
        if "azure.archive.ubuntu.com" not in fields.get("uris", ""):
            continue
        addresses = fields["uris"].split()
        replacements = [uri(value) for value in addresses]
        if not fields.get("types") or not fields.get("suites") or not fields.get("components"):
            raise ValueError("Incomplete Azure source")
        if fields.get("enabled", "yes").lower() == "no":
            continue
        if any(suite.endswith("-security") for suite in fields["suites"].split()):
            continue
        in_uris = False
        lines = []
        for line in block.splitlines(keepends=True):
            if line.lstrip().startswith("#"):
                lines.append(line)
                continue
            if line and not line[0].isspace():
                in_uris = line.lower().startswith("uris:")
            if in_uris:
                prefix = ""
                if line.lower().startswith("uris:"):
                    prefix, line = line[:5], line[5:]
                for old, new in zip(addresses, replacements):
                    line = re.sub(r"(?<!\S)" + re.escape(old) + r"(?!\S)", lambda _: new, line)
                line = prefix + line
            lines.append(line)
        blocks[i] = "".join(lines)
    return "".join(blocks)


def configure(root):
    root = Path(root)
    paths = [root / "sources.list"]
    directory = root / "sources.list.d"
    if directory.is_symlink():
        raise ValueError("Refusing a symlink source directory")
    if directory.exists():
        paths += sorted(directory.glob("*.list")) + sorted(directory.glob("*.sources"))
    plan = []
    for path in paths:
        if path.is_symlink():
            raise ValueError("Refusing a symlink source file")
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError("Source is not a regular file")
        before = path.read_bytes()
        after = transform(before.decode("utf-8"), path.suffix == ".sources").encode("utf-8")
        if before != after:
            plan.append((path, before, after, stat.S_IMODE(path.stat().st_mode)))
    # Validate every source before touching any file. Atomic per-file replacement.
    for path, before, after, mode in plan:
        if path.read_bytes() != before:
            raise ValueError("Source changed during validation")
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(after)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), mode)
            os.replace(temporary, path)
            if path.read_bytes() != after:
                raise ValueError("Source readback does not match the selected archive")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return len(plan)


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        raise SystemExit("Only disposable GitHub Linux CI runners are supported")
    print(f"Official HTTPS Ubuntu archive: updated {configure('/etc/apt')} source files")
