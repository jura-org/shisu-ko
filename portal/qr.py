# SPDX-License-Identifier: Apache-2.0
"""A small QR code encoder (ISO/IEC 18004), stdlib only.

Byte mode, error correction L or M, versions 1 to 10: enough for the portal's pairing URL.
qr_matrix() gives the modules; qr_text() draws them for a terminal with Unicode half blocks.
"""

from __future__ import annotations

# Per version and level: (EC codewords per block, [(block count, data codewords per block), ...]).
BLOCKS = {
    "L": {
        1: (7, [(1, 19)]),
        2: (10, [(1, 34)]),
        3: (15, [(1, 55)]),
        4: (20, [(1, 80)]),
        5: (26, [(1, 108)]),
        6: (18, [(2, 68)]),
        7: (20, [(2, 78)]),
        8: (24, [(2, 97)]),
        9: (30, [(2, 116)]),
        10: (18, [(2, 68), (2, 69)]),
    },
    "M": {
        1: (10, [(1, 16)]),
        2: (16, [(1, 28)]),
        3: (26, [(1, 44)]),
        4: (18, [(2, 32)]),
        5: (24, [(2, 43)]),
        6: (16, [(4, 27)]),
        7: (18, [(4, 31)]),
        8: (22, [(2, 38), (2, 39)]),
        9: (22, [(3, 36), (2, 37)]),
        10: (26, [(4, 43), (1, 44)]),
    },
}

ALIGNMENT = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

ECC_BITS = {"L": 0b01, "M": 0b00}
MAX_VERSION = 10

# GF(256) with the primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D).
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def rs_generator(degree: int) -> list[int]:
    """Coefficients of (x - a^0)(x - a^1)...(x - a^(degree-1)), highest power first."""
    poly = [1]
    for i in range(degree):
        nxt = poly + [0]
        for j, coef in enumerate(poly):
            nxt[j + 1] ^= gf_mul(coef, EXP[i])
        poly = nxt
    return poly


def rs_remainder(data: list[int], degree: int) -> list[int]:
    """The EC codewords: data * x^degree mod the generator (long division)."""
    gen = rs_generator(degree)
    rem = list(data) + [0] * degree
    for i in range(len(data)):
        factor = rem[i]
        if factor:
            for j in range(1, len(gen)):
                rem[i + j] ^= gf_mul(gen[j], factor)
    return rem[len(data):]


def data_capacity(version: int, ecc: str) -> int:
    """Data codewords (bytes) the symbol holds, mode header included."""
    return sum(count * size for count, size in BLOCKS[ecc][version][1])


def count_bits(version: int) -> int:
    return 8 if version <= 9 else 16


def choose_version(length: int, ecc: str) -> int:
    for version in range(1, MAX_VERSION + 1):
        if 4 + count_bits(version) + 8 * length <= 8 * data_capacity(version, ecc):
            return version
    raise ValueError(f"{length} bytes do not fit a version {MAX_VERSION} QR code at level {ecc}")


def encode_data(payload: bytes, version: int, ecc: str) -> list[int]:
    """Mode indicator, length, bytes, terminator and padding, as data codewords."""
    capacity = data_capacity(version, ecc) * 8
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> i) & 1 for i in range(width - 1, -1, -1))

    put(0b0100, 4)
    put(len(payload), count_bits(version))
    for byte in payload:
        put(byte, 8)
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity // 8:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    return words


def interleave(words: list[int], version: int, ecc: str) -> list[int]:
    """Split into blocks, add each block's EC codewords, interleave column by column."""
    ec_len, groups = BLOCKS[ecc][version]
    blocks = []
    pos = 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[pos:pos + size])
            pos += size
    ec_blocks = [rs_remainder(block, ec_len) for block in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec_len):
        out.extend(b[i] for b in ec_blocks)
    return out


def bch(value: int, poly: int, poly_bits: int) -> int:
    """Remainder of value * x^(degree of poly) divided by poly, over GF(2)."""
    degree = poly_bits - 1
    rem = value << degree
    for shift in range(rem.bit_length() - poly_bits, -1, -1):
        if rem >> (shift + degree) & 1:
            rem ^= poly << shift
    return rem


def format_bits(ecc: str, mask: int) -> int:
    data = ECC_BITS[ecc] << 3 | mask
    return (data << 10 | bch(data, 0x537, 11)) ^ 0x5412


def version_bits(version: int) -> int:
    return version << 12 | bch(version, 0x1F25, 13)


MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


class _Symbol:
    """A matrix under construction; `fixed` marks function modules the data must skip."""

    def __init__(self, version: int):
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, row: int, col: int, dark: bool) -> None:
        self.dark[row][col] = dark
        self.fixed[row][col] = True

    def draw_function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for row, col in ((0, 0), (0, n - 7), (n - 7, 0)):
            # Finder plus its light separator: rings by Chebyshev distance from the centre.
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    r, c = row + dr, col + dc
                    if 0 <= r < n and 0 <= c < n:
                        ring = max(abs(dr - 3), abs(dc - 3))
                        self.set(r, c, ring in (0, 1, 3))
        centres = ALIGNMENT[self.version]
        last = len(centres) - 1
        for i, r in enumerate(centres):
            for j, c in enumerate(centres):
                if (i, j) in ((0, 0), (0, last), (last, 0)):  # would overlap a finder
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.set(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
        self.draw_format(0)  # reserves the area; rewritten once the mask is chosen
        if self.version >= 7:
            bits = version_bits(self.version)
            for i in range(18):
                dark = bool(bits >> i & 1)
                a, b = n - 11 + i % 3, i // 3
                self.set(a, b, dark)
                self.set(b, a, dark)

    def draw_format(self, bits: int) -> None:
        n = self.size
        bit = [bool(bits >> i & 1) for i in range(15)]
        # Copy around the top-left finder: down column 8, then left along row 8.
        for i in range(6):
            self.set(i, 8, bit[i])
        self.set(7, 8, bit[6])
        self.set(8, 8, bit[7])
        self.set(8, 7, bit[8])
        for i in range(9, 15):
            self.set(8, 14 - i, bit[i])
        # Second copy: row 8 at the right edge, column 8 at the bottom edge.
        for i in range(8):
            self.set(8, n - 1 - i, bit[i])
        for i in range(8, 15):
            self.set(n - 15 + i, 8, bit[i])
        self.set(n - 8, 8, True)  # the dark module

    def place_data(self, codewords: list[int]) -> None:
        n = self.size
        total = len(codewords) * 8
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:  # skip the vertical timing column
                right = 5
            upward = (right + 1) & 2 == 0
            for vert in range(n):
                row = n - 1 - vert if upward else vert
                for col in (right, right - 1):
                    if not self.fixed[row][col]:
                        # Remainder bits past the last codeword stay light.
                        if i < total:
                            self.dark[row][col] = bool(codewords[i >> 3] >> (7 - (i & 7)) & 1)
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        test = MASKS[mask]
        for r in range(self.size):
            for c in range(self.size):
                if not self.fixed[r][c] and test(r, c):
                    self.dark[r][c] = not self.dark[r][c]


def penalty(matrix: list[list[bool]]) -> int:
    n = len(matrix)
    score = 0
    lines = matrix + [list(col) for col in zip(*matrix)]
    for line in lines:
        # N1: runs of five or more of one colour.
        run = 1
        for a, b in zip(line, line[1:]):
            if a == b:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
        # N3: 1:1:3:1:1 finder look-alikes with four light modules on one side;
        # outside the symbol counts as light.
        padded = [False] * 4 + line + [False] * 4
        for i in range(len(padded) - 10):
            window = padded[i:i + 11]
            if window == _N3_A or window == _N3_B:
                score += 40
    # N2: 2x2 blocks of one colour.
    for r in range(n - 1):
        for c in range(n - 1):
            v = matrix[r][c]
            if v == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3
    # N4: 10 points per 5% the dark share strays from 50%.
    dark = sum(map(sum, matrix))
    total = n * n
    score += 10 * (abs(dark * 20 - total * 10) // total)
    return score


_N3_A = [True, False, True, True, True, False, True, False, False, False, False]
_N3_B = _N3_A[::-1]


def qr_matrix(data: bytes | str, ecc: str = "M", mask: int | None = None) -> list[list[bool]]:
    """The QR code for data (byte mode) as rows of modules; True is dark.

    The mask with the lowest penalty is used unless one is given (0 to 7).
    """
    if ecc not in BLOCKS:
        raise ValueError(f"unsupported error correction level {ecc!r}")
    if mask is not None and mask not in range(8):
        raise ValueError(f"mask must be 0 to 7, not {mask!r}")
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    version = choose_version(len(payload), ecc)
    codewords = interleave(encode_data(payload, version, ecc), version, ecc)

    base = _Symbol(version)
    base.draw_function_patterns()
    base.place_data(codewords)

    best = None
    for candidate in range(8) if mask is None else [mask]:
        sym = _Symbol(version)
        sym.dark = [row[:] for row in base.dark]
        sym.fixed = base.fixed
        sym.apply_mask(candidate)
        sym.draw_format(format_bits(ecc, candidate))
        score = penalty(sym.dark) if mask is None else 0
        if best is None or score < best[0]:
            best = (score, sym.dark)
    return best[1]


def qr_text(data: bytes | str, ecc: str = "M", invert: bool = False, quiet: int = 4) -> str:
    """The QR code drawn with half blocks, two module rows per line, inside a quiet zone.

    Scanners want dark modules on a light ground. A terminal draws its glyphs in the light
    foreground over a dark background, so by default the light modules and the quiet zone are
    the blocks and the dark modules are spaces. invert=True is for a light-background terminal:
    the dark modules become the blocks. Rows past the bottom edge are left as background.
    """
    matrix = qr_matrix(data, ecc)
    size = len(matrix) + 2 * quiet

    def lit(r: int, c: int) -> bool:
        if r >= size:
            return False
        dark = False
        mr, mc = r - quiet, c - quiet
        if 0 <= mr < len(matrix) and 0 <= mc < len(matrix):
            dark = matrix[mr][mc]
        return dark if invert else not dark

    glyphs = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    lines = []
    for r in range(0, size, 2):
        lines.append("".join(glyphs[lit(r, c), lit(r + 1, c)] for c in range(size)))
    return "\n".join(lines)
