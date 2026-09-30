"""Fixed-purpose isolated JSON Schema evaluator. No diagnostic payload is printed."""
import json
import sys


def main():
    try:
        # Availability limits in addition to the parent's wall-clock deadline.
        # Windows has no resource module; the parent still kills and reaps.
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
            if sys.platform.startswith('linux'):
                resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        except ImportError:
            pass
        from jsonschema import Draft202012Validator
        from referencing import Registry
        from referencing.exceptions import NoSuchResource
        def deny_remote(uri):
            raise NoSuchResource(ref=uri)
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return 1
        body = json.loads(raw)
        Draft202012Validator(body['schema'], registry=Registry(retrieve=deny_remote)).validate(body['value'])
        return 0
    except BaseException:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
