"""Select the official HTTPS Ubuntu archive on disposable GitHub Linux runners.

The default strict-mirror policy preserves the historical exact-list contract.
Explicit direct-source mode replaces only a verified Ubuntu 24.04 amd64 source's
fixed mirror-file URI; the mirror list is never read, trusted or changed.
Neither policy changes signing keys, suites, components or APT budgets.
"""
import argparse
import os
from pathlib import Path
import platform
import re
import stat
import tempfile

OLD = {"http://azure.archive.ubuntu.com/ubuntu", "https://azure.archive.ubuntu.com/ubuntu"}
NEW = "https://archive.ubuntu.com/ubuntu"
MIRROR_URI = "mirror+file:/etc/apt/apt-mirrors.txt"
SECURITY = "https://security.ubuntu.com/ubuntu"
UBUNTU_KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
DIRECT_FIELDS = {"types", "uris", "suites", "components", "signed-by", "architectures", "enabled"}
DIRECT_SUITES = {"noble", "noble-updates", "noble-backports"}
DIRECT_COMPONENTS = {"main", "restricted", "universe", "multiverse"}
# Official actions/runner-images configure-apt-sources.sh, 144-byte fixture.
AZURE_MIRROR = "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
HTTPS_MIRRORS = ("https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
                 "https://security.ubuntu.com/ubuntu/\tpriority:3\n")


def transform_mirrorlist(text):
    # Keep existing official entries and metadata; do not create duplicate URIs.
    if text not in {AZURE_MIRROR + HTTPS_MIRRORS, HTTPS_MIRRORS}:
        raise ValueError("Unexpected hosted-runner mirror list; refusing to rewrite")
    return HTTPS_MIRRORS


def uri(value):
    if value.removesuffix("/") in OLD:
        return NEW + ("/" if value.endswith("/") else "")
    if "azure.archive.ubuntu.com" in value:
        raise ValueError("Unexpected Azure archive URI; refusing to rewrite")
    return value


def deb822_fields(block):
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
    return fields


def active_addresses(text, deb822):
    if deb822:
        for block in re.split(r"\r?\n[ \t]*\r?\n", text):
            fields = deb822_fields(block)
            if fields.get("enabled", "yes").lower() == "no":
                continue
            yield from fields.get("uris", "").split()
    else:
        for line in text.splitlines():
            # Only the actual address after deb/options, never a trailing comment.
            match = re.match(r"^\s*deb(?:-src)?\s+(?:\[[^\]\n]+\]\s+)?([^\s#]+)\s+[^\s#]+", line)
            if match:
                yield match[1]


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
        fields = deb822_fields(block)
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


def require_direct_environment():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("Direct-source requires Linux x86_64")
    try:
        release = platform.freedesktop_os_release()
    except OSError as error:
        raise ValueError("Direct-source cannot establish the OS release") from error
    if (release.get("ID"), release.get("VERSION_ID"), release.get("VERSION_CODENAME")) != (
            "ubuntu", "24.04", "noble"):
        raise ValueError("Direct-source requires Ubuntu 24.04 noble")


def direct_target(fields):
    """A closed source profile, independent of any cloud mirror-list format."""
    unsupported = fields.keys() - DIRECT_FIELDS
    if unsupported:
        # Diagnose the category, never print source values, arbitrary field
        # names, URLs or possible credentials into public CI logs.
        raise ValueError(f"Direct-source has unsupported source fields ({len(unsupported)})")
    if fields.get("enabled", "yes").lower() != "yes":
        raise ValueError("Direct-source has an unsupported Enabled value")
    if fields.get("uris", "").split() != [MIRROR_URI]:
        raise ValueError("Direct-source requires the single exact hosted-runner mirror URI")
    kinds = fields.get("types", "").split()
    if not kinds or len(kinds) != len(set(kinds)) or not set(kinds) <= {"deb", "deb-src"}:
        raise ValueError("Direct-source Types must be deb and/or deb-src without duplicates")
    if fields.get("signed-by", "").split() != [UBUNTU_KEYRING]:
        raise ValueError("Direct-source requires the official Ubuntu Signed-By keyring")
    if "architectures" in fields and fields["architectures"].split() != ["amd64"]:
        raise ValueError("Direct-source Architectures must be amd64 when specified")
    components = fields.get("components", "").split()
    if (not components or len(components) != len(set(components)) or
            not set(components) <= DIRECT_COMPONENTS):
        raise ValueError("Direct-source has missing, duplicate or unknown Ubuntu Components")
    suites = fields.get("suites", "").split()
    if not suites or len(suites) != len(set(suites)):
        raise ValueError("Direct-source has missing or duplicate Suites")
    if suites == ["noble-security"]:
        return SECURITY
    if "noble-security" in suites:
        raise ValueError("Direct-source cannot mix security and ordinary Suites")
    if not set(suites) <= DIRECT_SUITES:
        raise ValueError("Direct-source has unknown Suites; only noble, noble-updates and noble-backports are supported")
    return NEW


def direct_deb822(text):
    blocks = re.split(r"(\r?\n[ \t]*\r?\n)", text)
    for index in range(0, len(blocks), 2):
        block = blocks[index]
        fields = deb822_fields(block)
        if fields.get("enabled", "yes").lower() == "no":
            continue
        if MIRROR_URI not in fields.get("uris", "").split():
            continue
        target = direct_target(fields)
        in_uris = False
        lines = []
        for line in block.splitlines(keepends=True):
            if line.lstrip().startswith("#"):
                lines.append(line)
                continue
            if line and not line[0].isspace():
                in_uris = line.lower().startswith("uris:")
            if in_uris:
                prefix, value = (line[:5], line[5:]) if line.lower().startswith("uris:") else ("", line)
                value = re.sub(r"(?<!\S)" + re.escape(MIRROR_URI) + r"(?!\S)", lambda _: target, value)
                line = prefix + value
            lines.append(line)
        blocks[index] = "".join(lines)
    return "".join(blocks)


def direct_legacy(text):
    lines = []
    for line in text.splitlines(keepends=True):
        address = next(active_addresses(line, False), None)
        if address != MIRROR_URI:
            # A truncated active source must not hide its unprocessed mirror
            # reference from the common parser. Comments/options are not URIs.
            if (address is None or address.startswith("[")) and re.search(
                    r"(?<!\S)mirror\+file:", line.split("#", 1)[0], re.I):
                raise ValueError("Malformed direct-source legacy mirror entry")
            lines.append(line)
            continue
        match = re.match(r"^(?P<prefix>\s*(?P<type>deb(?:-src)?)\s+"
                         r"(?:\[(?P<options>[^\]\r\n]+)\]\s+)?)(?P<uri>\S+)\s+"
                         r"(?P<suite>\S+)(?P<rest>.*)$", line.rstrip("\r\n"))
        if not match:
            raise ValueError("Malformed direct-source legacy entry")
        options = {}
        for option in (match["options"] or "").split():
            setting = re.fullmatch(r"(arch|signed-by)=([^\s]+)", option)
            if not setting or setting[1] in options:
                raise ValueError("Direct-source has unsupported or duplicate legacy options")
            options[setting[1]] = setting[2]
        fields = {"types": match["type"], "uris": address, "suites": match["suite"],
                  "components": match["rest"].split("#", 1)[0].strip(),
                  "signed-by": options.get("signed-by", "")}
        if "arch" in options:
            fields["architectures"] = options["arch"].replace(",", " ")
        target = direct_target(fields)
        lines.append(line[:match.start("uri")] + target + line[match.end("uri"):])
    return "".join(lines)


def configure(root, *, mode="strict-mirror"):
    if mode not in {"strict-mirror", "direct-source"}:
        raise ValueError("Unknown archive-selection mode")
    if mode == "direct-source":
        require_direct_environment()
    root = Path(root)
    paths = [root / "sources.list"]
    directory = root / "sources.list.d"
    if directory.is_symlink():
        raise ValueError("Refusing a symlink source directory")
    if directory.exists():
        paths += sorted(directory.glob("*.list")) + sorted(directory.glob("*.sources"))
    plan = []
    mirror_referenced = False
    for path in paths:
        if path.is_symlink():
            raise ValueError("Refusing a symlink source file")
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError("Source is not a regular file")
        before = path.read_bytes()
        text = before.decode("utf-8")
        for address in active_addresses(text, path.suffix == ".sources"):
            if address.startswith("mirror+file:") or mode == "direct-source" and address.lower().startswith("mirror+file:"):
                if address != MIRROR_URI:
                    raise ValueError("Unknown mirror file URI; refusing to follow")
                mirror_referenced = True
        if mode == "direct-source":
            selected = direct_deb822(text) if path.suffix == ".sources" else direct_legacy(text)
            if any(address.lower().startswith("mirror+file:") for address in
                   active_addresses(selected, path.suffix == ".sources")):
                raise ValueError("Direct-source left an active mirror-file reference")
        else:
            selected = transform(text, path.suffix == ".sources")
        after = selected.encode("utf-8")
        if before != after:
            plan.append((path, before, after, stat.S_IMODE(path.stat().st_mode)))
    if mirror_referenced and mode == "strict-mirror":
        # Never follow a source-supplied path. Only this known runner file is read.
        path = root / "apt-mirrors.txt"
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or unsafe hosted-runner mirror list")
        before = path.read_bytes()
        after = transform_mirrorlist(before.decode("utf-8")).encode("utf-8")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("strict-mirror", "direct-source"), default="strict-mirror")
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        raise SystemExit("Only disposable GitHub Linux CI runners are supported")
    changed = configure('/etc/apt', mode=args.mode)
    print(f"Official HTTPS Ubuntu archive ({args.mode}): updated {changed} source files")


if __name__ == "__main__":
    main()
