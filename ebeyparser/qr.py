"""A tiny QR code encoder for the startup link: byte mode, versions 1–6, error correction M or L.

Enough for http://<ip-or-name>:8000/?token=… (up to 134 bytes). Printed in the terminal with
half-block characters, so a headless server's log shows a code the phone can scan.
Follows ISO/IEC 18004 (and Project Nayuki's reference implementation); `render()` returns None
when the text does not fit — the link is printed anyway.
"""

from __future__ import annotations

# version -> (data codewords per block, number of blocks, EC codewords per block) for M and L
_BLOCKS = {
    "M": {1: (16, 1, 10), 2: (28, 1, 16), 3: (44, 1, 26), 4: (32, 2, 18), 5: (43, 2, 24), 6: (27, 4, 16)},
    "L": {1: (19, 1, 7), 2: (34, 1, 10), 3: (55, 1, 15), 4: (80, 1, 20), 5: (108, 1, 26), 6: (68, 2, 18)},
}
_FORMAT_BITS = {"L": 1, "M": 0}
_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)

# GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1
_EXP = [0] * 512
_LOG = [0] * 256
_v = 1
for _i in range(255):
    _EXP[_i] = _v
    _LOG[_v] = _i
    _v <<= 1
    if _v & 0x100:
        _v ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_ecc(data: list[int], degree: int) -> list[int]:
    gen = [1]
    for i in range(degree):  # (x - a^0)(x - a^1)...(x - a^(degree-1))
        gen = [c ^ _mul(g, _EXP[i]) for c, g in zip([*gen, 0], [0, *gen])]
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = [*rem[1:], 0]
        for i in range(degree):
            rem[i] ^= _mul(gen[i + 1], factor)
    return rem


def _codewords(data: bytes, version: int, ecl: str) -> list[int] | None:
    per_block, blocks, ec_len = _BLOCKS[ecl][version]
    capacity = per_block * blocks
    if len(data) > capacity - 2:  # 4 bits mode + 8 bits count (+ terminator) for versions 1-9
        return None
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in range(7, -1, -1)]
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    chunks = [words[i * per_block:(i + 1) * per_block] for i in range(blocks)]
    eccs = [_rs_ecc(c, ec_len) for c in chunks]
    return ([c[i] for i in range(per_block) for c in chunks]
            + [e[i] for i in range(ec_len) for e in eccs])


class _Matrix:
    def __init__(self, version: int) -> None:
        self.size = size = 17 + 4 * version
        self.dark = [[False] * size for _ in range(size)]
        self.fixed = [[False] * size for _ in range(size)]
        for i in range(size):  # timing patterns
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finder patterns + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        if version >= 2:  # one alignment pattern for versions 2-6
            c = size - 7
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    self.set(c + dx, c + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits("M", 0)  # reserve the format areas (redrawn later)

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def format_bits(self, ecl: str, mask: int) -> None:
        data = _FORMAT_BITS[ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        size = self.size
        for i in range(6):
            self.set(8, i, bit[i])
        self.set(8, 7, bit[6])
        self.set(8, 8, bit[7])
        self.set(7, 8, bit[8])
        for i in range(9, 15):
            self.set(14 - i, 8, bit[i])
        for i in range(8):
            self.set(size - 1 - i, 8, bit[i])
        for i in range(8, 15):
            self.set(8, size - 15 + i, bit[i])
        self.set(8, size - 8, True)  # the dark module

    def place(self, codewords: list[int]) -> None:
        size, i, total = self.size, 0, len(codewords) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def masked(self, mask: int, ecl: str) -> list[list[bool]]:
        cond = _MASKS[mask]
        out = [[d ^ (not f and cond(x, y)) for x, (d, f) in enumerate(zip(row, frow))]
               for y, (row, frow) in enumerate(zip(self.dark, self.fixed))]
        saved = [r[:] for r in self.dark]
        self.dark = out
        self.format_bits(ecl, mask)
        out, self.dark = self.dark, saved
        return out


def _penalty(m: list[list[bool]]) -> int:
    size = len(m)
    score = 0
    lines = m + [list(col) for col in zip(*m)]
    finder = [True, False, True, True, True, False, True]
    for line in lines:
        run, prev = 0, None
        for cell in line:
            run = run + 1 if cell == prev else 1
            prev = cell
            if run == 5:
                score += 3
            elif run > 5:
                score += 1
        padded = [False] * 4 + line + [False] * 4
        for i in range(len(padded) - 6):
            if padded[i:i + 7] == finder and (not any(padded[i - 4:i]) or not any(padded[i + 7:i + 11])):
                score += 40
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    return score + 10 * (abs(dark * 20 - size * size * 10) // (size * size))


def encode(text: str, *, ecl: str | None = None, mask: int | None = None) -> list[list[bool]] | None:
    """QR matrix (True = dark) for `text`, or None when it is longer than 134 bytes.
    Picks the smallest version, M when it fits in version 6 else L, and the best mask."""
    data = text.encode("utf-8")
    for level in ([ecl] if ecl else ["M", "L"]):
        for version in range(1, 7):
            words = _codewords(data, version, level)
            if words is None:
                continue
            matrix = _Matrix(version)
            matrix.place(words)
            if mask is not None:
                return matrix.masked(mask, level)
            return min((matrix.masked(m, level) for m in range(8)), key=_penalty)
    return None


def render(text: str, *, border: int = 2) -> list[str] | None:
    """Terminal lines (two module rows per line, light modules drawn: for a dark background)."""
    matrix = encode(text)
    if matrix is None:
        return None
    size = len(matrix) + 2 * border
    light = [[True] * size for _ in range(size)]
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            light[y + border][x + border] = not dark
    light.append([False] * size)  # odd height: the last half-row is terminal background
    chars = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    return ["".join(chars[(top, bottom)] for top, bottom in zip(light[y], light[y + 1]))
            for y in range(0, size, 2)]


__all__ = ["encode", "render"]
