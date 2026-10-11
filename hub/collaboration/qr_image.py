"""Bounded, dependency-free QR Model 2 encoding for local login display.

Only byte mode with UTF-8 ECI (assignment 26), correction level L, and versions
5, 10, 15, 20 are supported. The maximum is 857 UTF-8 bytes. This intentionally
small implementation never fetches a URL or puts the payload into SVG markup.
Tables and placement follow ISO/IEC 18004 QR Model 2; the correction-level and
version capacities are cross-checkable at https://www.qrcode.com/en/about/version.html.
"""
from __future__ import annotations

from itertools import groupby

MAX_PAYLOAD_BYTES = 857

# version: (data codewords, parity words per block, number of blocks, alignment)
_PROFILES = {
    5: (108, 26, 1, (6, 30)),
    10: (274, 18, 4, (6, 28, 50)),
    15: (523, 22, 6, (6, 26, 48, 70)),
    20: (861, 28, 8, (6, 34, 62, 90)),
}


def _multiply(a: int, b: int) -> int:
    """GF(256) multiplication using the QR primitive polynomial x^8+x^4+x^3+x^2+1."""
    value = 0
    while b:
        if b & 1:
            value ^= a
        b >>= 1
        a <<= 1
        if a & 256:
            a ^= 0x11D
    return value


def _parity(data: bytes, count: int) -> bytes:
    # Build the monic generator product (x + alpha^i), alpha = 2.
    generator = [1]
    root = 1
    for _ in range(count):
        product = [0] * (len(generator) + 1)
        for index, coefficient in enumerate(generator):
            product[index] ^= coefficient
            product[index + 1] ^= _multiply(coefficient, root)
        generator = product
        root = _multiply(root, 2)
    dividend = list(data) + [0] * count
    for index in range(len(data)):
        coefficient = dividend[index]
        if coefficient:
            for offset, factor in enumerate(generator):
                dividend[index + offset] ^= _multiply(coefficient, factor)
    return bytes(dividend[-count:])


def _data_words(payload: bytes, version: int) -> bytes:
    capacity = _PROFILES[version][0]
    length_width = 8 if version < 10 else 16
    # ECI 26 identifies UTF-8; next segment is byte mode.
    bits = "0111" + format(26, "08b") + "0100" + format(len(payload), f"0{length_width}b")
    bits += "".join(format(value, "08b") for value in payload)
    bits += "0" * min(4, capacity * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = bytearray(int(bits[index:index + 8], 2) for index in range(0, len(bits), 8))
    while len(words) < capacity:
        words.append(0xEC if (len(words) - len(bits) // 8) % 2 == 0 else 0x11)
    return bytes(words)


def _codewords(payload: bytes, version: int) -> bytes:
    words = _data_words(payload, version)
    capacity, parity_count, block_count, _ = _PROFILES[version]
    short_size, long_count = divmod(capacity, block_count)
    blocks = []
    cursor = 0
    for index in range(block_count):
        length = short_size + (index >= block_count - long_count)
        blocks.append(words[cursor:cursor + length])
        cursor += length
    parity = [_parity(block, parity_count) for block in blocks]
    result = bytearray()
    for column in range(short_size + 1):
        result.extend(block[column] for block in blocks if column < len(block))
    for column in range(parity_count):
        result.extend(block[column] for block in parity)
    return bytes(result)


def _bch(value: int, degree: int, polynomial: int) -> int:
    remainder = value << degree
    while remainder.bit_length() >= polynomial.bit_length():
        remainder ^= polynomial << (remainder.bit_length() - polynomial.bit_length())
    return (value << degree) | remainder


def _format(matrix: list[list[bool | None]], mask: int) -> None:
    size = len(matrix)
    bits = _bch(8 | mask, 10, 0x537) ^ 0x5412  # L = binary 01.
    first = [(8, y) for y in range(6)] + [(8, 7), (8, 8), (7, 8)]
    first += [(x, 8) for x in range(5, -1, -1)]
    second = [(size - 1 - x, 8) for x in range(8)]
    second += [(8, y) for y in range(size - 7, size)]
    for positions in (first, second):
        for index, (x, y) in enumerate(positions):
            matrix[y][x] = bool(bits & (1 << index))
    matrix[size - 8][8] = True


def _functions(version: int) -> list[list[bool | None]]:
    size = version * 4 + 17
    matrix = [[None for _ in range(size)] for _ in range(size)]
    for index in range(size):
        matrix[6][index] = matrix[index][6] = index % 2 == 0
    for center_x, center_y in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = center_x + dx, center_y + dy
                if 0 <= x < size and 0 <= y < size:
                    radius = max(abs(dx), abs(dy))
                    matrix[y][x] = radius in (0, 1, 3)
    alignment = _PROFILES[version][3]
    corners = {(6, 6), (6, alignment[-1]), (alignment[-1], 6)}
    for center_x in alignment:
        for center_y in alignment:
            if (center_x, center_y) in corners:
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    matrix[center_y + dy][center_x + dx] = max(abs(dx), abs(dy)) != 1
    _format(matrix, 0)
    if version >= 7:
        bits = _bch(version, 12, 0x1F25)
        for index in range(18):
            x, y = size - 11 + index % 3, index // 3
            matrix[y][x] = matrix[x][y] = bool(bits & (1 << index))
    return matrix


def _mask(mask: int, x: int, y: int) -> bool:
    rules = (
        (x + y) % 2,
        y % 2,
        x % 3,
        (x + y) % 3,
        (y // 2 + x // 3) % 2,
        (x * y) % 2 + (x * y) % 3,
        ((x * y) % 2 + (x * y) % 3) % 2,
        ((x + y) % 2 + (x * y) % 3) % 2,
    )
    return rules[mask] == 0


def _penalty(matrix: list[list[bool]]) -> int:
    size = len(matrix)
    score = 0
    lines = list(matrix) + [list(column) for column in zip(*matrix)]
    for line in lines:
        for _, group in groupby(line):
            run = len(list(group))
            if run >= 5:
                score += run - 2
        # Include the quiet zone when checking finder-like patterns.
        text = "0000" + "".join("1" if value else "0" for value in line) + "0000"
        for index in range(len(text) - 10):
            if text[index:index + 11] in ("00001011101", "10111010000"):
                score += 40
    for y in range(size - 1):
        for x in range(size - 1):
            if matrix[y][x] == matrix[y][x + 1] == matrix[y + 1][x] == matrix[y + 1][x + 1]:
                score += 3
    dark = sum(sum(row) for row in matrix)
    score += (abs(dark * 20 - size * size * 10) // (size * size)) * 10
    return score


def qr_matrix(payload: str) -> list[list[bool]]:
    """Return QR modules, excluding the quiet zone; invalid/oversize input raises ValueError."""
    if not isinstance(payload, str) or not payload or len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("Invalid QR payload")
    try:
        data = payload.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid QR payload") from None
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError("QR payload exceeds local capacity")
    version = next(
        version for version, profile in _PROFILES.items()
        if 12 + 4 + (8 if version < 10 else 16) + len(data) * 8 <= profile[0] * 8
    )
    functions = _functions(version)
    matrix = [row[:] for row in functions]
    stream = "".join(format(word, "08b") for word in _codewords(data, version))
    bit_index = 0
    size = len(matrix)
    right, upwards = size - 1, True
    while right > 0:
        if right == 6:
            right = 5
        rows = range(size - 1, -1, -1) if upwards else range(size)
        for y in rows:
            for x in (right, right - 1):
                if functions[y][x] is None:
                    matrix[y][x] = bit_index < len(stream) and stream[bit_index] == "1"
                    bit_index += 1
        upwards = not upwards
        right -= 2
    if bit_index < len(stream):
        raise ValueError("QR encoding failed")
    candidates = []
    for mask in range(8):
        candidate = [
            [bool(value) ^ (functions[y][x] is None and _mask(mask, x, y))
             for x, value in enumerate(row)]
            for y, row in enumerate(matrix)
        ]
        _format(candidate, mask)
        candidates.append(candidate)
    return min(candidates, key=_penalty)


def qr_svg(payload: str) -> str:
    """Return an inert SVG with a mandatory four-module white quiet zone."""
    matrix = qr_matrix(payload)
    size = len(matrix) + 8
    path = "".join(
        f"M{x + 4},{y + 4}h1v1h-1z"
        for y, row in enumerate(matrix)
        for x, dark in enumerate(row) if dark
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        'shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path d="{path}" fill="#000"/></svg>'
    )
