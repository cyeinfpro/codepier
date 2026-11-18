"""Fixed-purpose, process-isolated validator with bounded, newline-framed requests."""

import json
import math
import sys

LIMIT = 2 * 1024 * 1024


def main():
    try:
        import resource
    except ImportError:
        resource = None
    if resource is not None:
        resource.setrlimit(resource.RLIMIT_CPU, (3, 256))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    def deny_remote(uri):
        raise NoSuchResource(ref=uri)

    def validate(line):
        body = json.loads(line)
        Draft202012Validator(body["schema"], registry=Registry(retrieve=deny_remote)).validate(
            body["value"]
        )

    for _ in range(64):
        line = sys.stdin.buffer.readline(LIMIT + 2)
        if not line:
            return 0
        if not line.endswith(b"\n") or len(line) > LIMIT + 1:
            return 1
        if resource is not None:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu = math.ceil(usage.ru_utime + usage.ru_stime)
            resource.setrlimit(resource.RLIMIT_CPU, (min(cpu + 3, 256), 256))
        try:
            validate(line)
            reply = b"1\n"
        except Exception:
            reply = b"0\n"
        del line
        sys.stdout.buffer.write(reply)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
