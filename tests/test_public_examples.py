"""Public examples use synthetic display data; no private deny-list is published."""
import ast
from pathlib import Path
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1]


def test_shared_display_fixture_uses_synthetic_project_names():
    tree = ast.parse((ROOT / "tests/support.py").read_text())
    names = {node.value.right.value for node in ast.walk(tree)
             if isinstance(node, ast.Assign) and isinstance(node.value, ast.BinOp)
             and isinstance(node.value.left, ast.Attribute) and node.value.left.attr == "root"
             and isinstance(node.value.right, ast.Constant)
             and isinstance(node.value.right.value, str)}
    assert names == {"ProjectAlpha", "ProjectBeta", "ProjectGamma"}
    devices = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "post":
            continue
        if not isinstance(node.args[0], ast.Constant) or node.args[0].value != "/api/devices":
            continue
        body = next(keyword.value for keyword in node.keywords if keyword.arg == "json")
        devices.extend(value.value for key, value in zip(body.keys, body.values)
                       if isinstance(key, ast.Constant) and key.value == "name")
    assert devices == ["Demo Device"]


def test_user_facing_alias_example_is_synthetic():
    assert 'placeholder="ProjectAlpha"' in (ROOT / "web/app.js").read_text()
    assert "e.g. ProjectAlpha" in (ROOT / "shared/contracts.py").read_text()


def test_readme_demo_png_has_no_hidden_metadata():
    raw = (ROOT / "web/readme/overview.png").read_bytes()
    assert raw[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10])
    offset = 8
    kinds = []
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", raw[offset + 8 + length:offset + 12 + length])[0]
        assert crc == zlib.crc32(kind + data) & 0xffffffff
        assert kind in {b"IHDR", b"IDAT", b"IEND"}
        kinds.append(kind)
        offset += length + 12
    assert offset == len(raw) and kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
