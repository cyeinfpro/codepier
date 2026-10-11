"""Portable contracts and decoder-verified golden vectors for local QR images.

The golden matrices were independently decoded with macOS Vision
VNDetectBarcodesRequest (QR symbology), including UTF-8 and every version boundary.
Only synthetic payloads appear here. The runtime encoder has no image/QR library.
"""
import hashlib
import re
import xml.etree.ElementTree as ET

import pytest

from hub.collaboration.qr_image import (
    MAX_PAYLOAD_BYTES,
    _bch,
    _codewords,
    _data_words,
    _functions,
    _parity,
    qr_matrix,
    qr_svg,
)


# Each expected matrix was decoded back to the exact payload by Apple Vision.
VECTORS = [
    ("https://weixin.example/login?token=synthetic-acceptance-only", 5,
     "eff19ec50410598b5afd3b5391a8e5449fd00607b1d8db6bed4430e4602782e3"),
    ("https://example.invalid/微信扫码?nonce=🔒", 5,
     "1a455f6cdde738e2c3b62ba3fcdeb6def10198b49c46dcfd0361420eac8064e6"),
    ("A" * 105, 5, "8dd9be10b4822b9a9116a382c17eea55869289759390a7698ba16ae65a558bb8"),
    ("A" * 106, 10, "2564ad7cbc1b5159d7a7d25bc57133863d47e90eaff3061331a15d47a95f565b"),
    ("B" * 270, 10, "367da280ebb4cc95132c5f56b95ffeb0d8f9c3ebf09b9e878d1864bbb68b6697"),
    ("B" * 271, 15, "f8d9968a1ea97f77d30b9d0342911d9f3833324b210eecc4251174fa07337533"),
    ("C" * 519, 15, "b7022e966d498a9c366be6fe838b35b42bdd7005eda7a11e9e907153e29fc55b"),
    ("C" * 520, 20, "bb7eb91aac74c3b565211e81e1e38fcc72b75ce9266126959f256c83e7e2ba17"),
    ("D" * 857, 20, "b74a648019b2df88955a48f0276ed292becda4f19d474b1f9ab615143d2c34e6"),
    ('<script>alert("inert")</script>', 5,
     "3d5feac4e6160250f5cb29a58e44d61ef3763a5fd1e59059e532f7e4a116ba90"),
]


@pytest.mark.parametrize("payload,version,digest", VECTORS)
def test_decoder_verified_matrix_golden(payload, version, digest):
    matrix = qr_matrix(payload)
    size = 17 + 4 * version
    assert len(matrix) == size
    assert all(len(row) == size and all(type(bit) is bool for bit in row) for row in matrix)
    assert hashlib.sha256(bytes(bit for row in matrix for bit in row)).hexdigest() == digest
    assert matrix[size - 8][8]  # Fixed dark module.


@pytest.mark.parametrize("payload", [None, b"data", "", 12, "x" * 858, "微" * 286, "\ud800"])
def test_invalid_input_is_bounded_and_not_echoed(payload):
    with pytest.raises(ValueError) as raised:
        qr_svg(payload)
    assert str(raised.value) in ("Invalid QR payload", "QR payload exceeds local capacity")


def test_exact_utf8_limit():
    assert MAX_PAYLOAD_BYTES == 857
    assert len(qr_matrix("微" * 285 + "ab")) == 97
    with pytest.raises(ValueError):
        qr_matrix("微" * 285 + "abc")


@pytest.mark.parametrize("version,capacity,total,remainder", [
    (5, 108, 134, 7), (10, 274, 346, 0), (15, 523, 655, 3), (20, 861, 1085, 3),
])
def test_bit_capacity_and_reserved_patterns(version, capacity, total, remainder):
    assert len(_data_words(b"a", version)) == capacity
    assert len(_codewords(b"a", version)) == total
    functions = _functions(version)
    assert sum(bit is None for row in functions for bit in row) == total * 8 + remainder
    size = len(functions)
    for x, y in ((3, 3), (size - 4, 3), (3, size - 4)):
        assert functions[y][x] is True
        assert functions[y][x + 2] is False
        assert functions[y][x + 3] is True


def test_utf8_eci_byte_length_terminator_and_padding():
    payload = "微🔒".encode("utf-8")
    for version in (5, 10):
        words = _data_words(payload, version)
        prefix = b"\x71\xa4" + len(payload).to_bytes(1 if version == 5 else 2, "big")
        assert words.startswith(prefix + payload + b"\x00\xec\x11")
    assert len(_data_words(b"a" * 105, 5)) == 108
    assert len(_data_words(b"a" * 857, 20)) == 861


def test_bch_known_format_and_version_vectors():
    assert _bch(8, 10, 0x537) ^ 0x5412 == 0x77C4  # L, mask 0.
    assert _bch(7, 12, 0x1F25) == 0x07C94
    assert _bch(10, 12, 0x1F25) == 0x0A4D3


@pytest.mark.parametrize("count", [18, 22, 26, 28])
def test_reed_solomon_syndromes_with_independent_log_field(count):
    # Independently evaluate every generator root using exponent/log tables,
    # rather than calling the production polynomial multiplication routine.
    powers, logs, value = [], {}, 1
    for index in range(255):
        powers.append(value)
        logs[value] = index
        value *= 2
        if value >= 256:
            value ^= 285

    def multiply(a, b):
        return powers[(logs[a] + logs[b]) % 255] if a and b else 0

    for data in (b"", b"login", bytes(range(108))):
        encoded = data + _parity(data, count)
        for exponent in range(count):
            syndrome = 0
            for coefficient in encoded:
                syndrome = multiply(syndrome, powers[exponent]) ^ coefficient
            assert syndrome == 0


def test_svg_contains_only_inert_geometry_and_a_four_module_quiet_zone():
    payload = '<svg onload="alert(1)"><script>&login-secret</script>'
    svg = qr_svg(payload)
    assert "login-secret" not in svg and "onload" not in svg and "<script" not in svg
    root = ET.fromstring(svg)
    namespace = "{http://www.w3.org/2000/svg}"
    assert root.tag == namespace + "svg"
    assert root.attrib == {"viewBox": "0 0 45 45", "shape-rendering": "crispEdges"}
    assert [child.tag for child in root] == [namespace + "rect", namespace + "path"]
    assert root[0].attrib == {"width": "45", "height": "45", "fill": "#fff"}
    assert set(root[1].attrib) == {"d", "fill"} and root[1].attrib["fill"] == "#000"
    geometry = root[1].attrib["d"]
    pieces = re.findall(r"M(\d+),(\d+)h1v1h-1z", geometry)
    assert "".join(f"M{x},{y}h1v1h-1z" for x, y in pieces) == geometry
    actual = {(int(x) - 4, int(y) - 4) for x, y in pieces}
    expected = {(x, y) for y, row in enumerate(qr_matrix(payload)) for x, dark in enumerate(row) if dark}
    assert actual == expected
    assert all(0 <= x < 37 and 0 <= y < 37 for x, y in actual)
