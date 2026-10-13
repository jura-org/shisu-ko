# SPDX-License-Identifier: Apache-2.0
"""Tests for qr.py. The reference hashes are qrencode 4's output (byte mode, no margin) at the
mask qrencode picked, so each one pins the whole symbol: data, EC, interleaving and placement."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "qr.py"
_spec = importlib.util.spec_from_file_location("qr", _PATH)
qr = importlib.util.module_from_spec(_spec)
sys.modules["qr"] = qr
_spec.loader.exec_module(qr)

URL = "http://192.168.1.23:8791/pair?code=3f9aKq7Zx_2LmN0pQrStUvWxYz-01234"


def digest(matrix):
    text = "\n".join("".join("1" if m else "0" for m in row) for row in matrix)
    return hashlib.sha256(text.encode()).hexdigest()


def version_of(matrix):
    return (len(matrix) - 17) // 4


# (data, ecc, version, qrencode's mask, sha256 of qrencode's matrix)
REFERENCES = [
    ("hi", "M", 1, 2, "81240590f3a630b03aa70a8aaf521ec30c4911af3307da27eeb2126afcb5b39a"),
    ("こんにちは", "L", 1, 7, "5d131ff52a26367bfb93a13c4b081713d84ea05f9ff7eee28ae47c67788d7f42"),
    (URL, "L", 4, 6, "bae721c6c034f21df19a5d1160633f35ff7073274a396e678310fee196c2b861"),
    (URL, "M", 5, 6, "838b9cbfdff6f19d29506241d38b489b055f3f759b9efbb17c62473e996d0c52"),
    ("http://192.168.100.230:8791/pair?code=" + "A" * 80, "M", 7, 0,
     "a551cafbbd34d4346a39beac0da5ee6bbbbd2e58a443afb19e0041db0ab1a9f2"),
    ("x" * 200, "M", 10, 0, "1e21180d1f36a922d626c44d8ee71b15e778ed0b3f02150d91b337171f504845"),
    ("x" * 270, "L", 10, 0, "4d64ada4b9e0fb8b04c5a6f61213c6b28115e02f86545f10290b0aa18c8e0f37"),
]


@pytest.mark.parametrize("data, ecc, version, mask, expected", REFERENCES)
def test_matches_qrencode(data, ecc, version, mask, expected):
    matrix = qr.qr_matrix(data, ecc, mask=mask)
    assert version_of(matrix) == version
    assert digest(matrix) == expected


def test_hi_rows_match_qrencode():
    rows = [0x1FCF7F, 0x104D41, 0x175B5D, 0x17595D, 0x17535D, 0x105941, 0x1FD57F, 0x001700,
            0x17C17C, 0x0EA521, 0x066A9E, 0x1D0834, 0x1D4295, 0x0013C9, 0x1FC562, 0x105FC9,
            0x175124, 0x175D24, 0x17529C, 0x104C34, 0x1FD69E]
    # qrencode chose mask 2 here too, so this pins the penalty choice as well.
    matrix = qr.qr_matrix("hi", "M")
    assert [int("".join("1" if m else "0" for m in row), 2) for row in matrix] == rows


def test_str_is_utf8():
    assert qr.qr_matrix("こんにちは", "L") == qr.qr_matrix("こんにちは".encode("utf-8"), "L")


FORMAT_TABLE = {
    "L": [0x77C4, 0x72F3, 0x7DAA, 0x789D, 0x662F, 0x6318, 0x6C41, 0x6976],
    "M": [0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0],
}


@pytest.mark.parametrize("ecc", ["L", "M"])
def test_format_bits_match_standard_table(ecc):
    assert [qr.format_bits(ecc, mask) for mask in range(8)] == FORMAT_TABLE[ecc]


def test_version_bits_match_standard_table():
    assert [qr.version_bits(v) for v in (7, 8, 9, 10)] == [0x07C94, 0x085BC, 0x09A99, 0x0A4D3]


def test_reed_solomon_hello_world_1m():
    # ISO 18004's HELLO WORLD at 1-M: its data codewords and their ten EC codewords.
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert qr.rs_remainder(data, 10) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]


@pytest.mark.parametrize("degree", [7, 10, 18, 26, 30])
def test_codeword_has_every_generator_root(degree):
    data = list(range(1, 40))
    codeword = data + qr.rs_remainder(data, degree)
    for i in range(degree):
        value = 0
        for coef in codeword:  # Horner at alpha^i
            value = qr.gf_mul(value, qr.EXP[i]) ^ coef
        assert value == 0


def test_block_tables_add_up():
    totals = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172, 7: 196, 8: 242, 9: 292, 10: 346}
    for ecc, table in qr.BLOCKS.items():
        for version, (ec_len, groups) in table.items():
            blocks = sum(count for count, _ in groups)
            assert qr.data_capacity(version, ecc) + blocks * ec_len == totals[version], (ecc, version)


@pytest.mark.parametrize("ecc, limits", [
    ("L", [17, 32, 53, 78, 106, 134, 154, 192, 230, 271]),
    ("M", [14, 26, 42, 62, 84, 106, 122, 152, 180, 213]),
])
def test_version_boundaries(ecc, limits):
    for version, limit in enumerate(limits, start=1):
        assert version_of(qr.qr_matrix(b"a" * limit, ecc)) == version
        if version < 10:
            assert version_of(qr.qr_matrix(b"a" * (limit + 1), ecc)) == version + 1
    with pytest.raises(ValueError):
        qr.qr_matrix(b"a" * (limits[-1] + 1), ecc)


def test_rejects_unknown_level_and_mask():
    with pytest.raises(ValueError):
        qr.qr_matrix("x", "H")
    with pytest.raises(ValueError):
        qr.qr_matrix("x", "M", mask=8)


def finder_at(matrix, row, col):
    for dr in range(7):
        for dc in range(7):
            ring = max(abs(dr - 3), abs(dc - 3))
            if matrix[row + dr][col + dc] != (ring != 2):
                return False
    return True


@pytest.mark.parametrize("length", [1, 30, 60, 100, 120, 200])
def test_structure(length):
    matrix = qr.qr_matrix(b"z" * length)
    version = version_of(matrix)
    n = 17 + 4 * version
    assert len(matrix) == n and all(len(row) == n for row in matrix)
    assert finder_at(matrix, 0, 0)
    assert finder_at(matrix, 0, n - 7)
    assert finder_at(matrix, n - 7, 0)
    assert not finder_at(matrix, n - 7, n - 7)
    assert matrix[n - 8][8]  # the dark module
    for i in range(8, n - 8):
        assert matrix[6][i] == matrix[i][6] == (i % 2 == 0)
    # The two copies of the format information agree.
    first = [matrix[i][8] for i in range(6)] + [matrix[7][8], matrix[8][8], matrix[8][7]]
    first += [matrix[8][14 - i] for i in range(9, 15)]
    second = [matrix[8][n - 1 - i] for i in range(8)] + [matrix[n - 15 + i][8] for i in range(8, 15)]
    assert first == second


def test_version_information_blocks():
    matrix = qr.qr_matrix(b"v" * 120, "M")
    n = len(matrix)
    assert version_of(matrix) == 7
    bits = qr.version_bits(7)
    for i in range(18):
        expected = bool(bits >> i & 1)
        assert matrix[n - 11 + i % 3][i // 3] == expected
        assert matrix[i // 3][n - 11 + i % 3] == expected


def test_alignment_pattern_on_timing_row_for_version_7():
    matrix = qr.qr_matrix(b"v" * 120, "M")
    assert version_of(matrix) == 7
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            assert matrix[6 + dr][22 + dc] == (max(abs(dr), abs(dc)) != 1)


def test_penalty_picks_the_lowest_mask():
    chosen = qr.qr_matrix(URL, "M")
    scores = [qr.penalty(qr.qr_matrix(URL, "M", mask=m)) for m in range(8)]
    assert qr.penalty(chosen) == min(scores)


@pytest.mark.parametrize("quiet", [2, 4])
@pytest.mark.parametrize("invert", [False, True])
def test_text_shape(quiet, invert):
    matrix = qr.qr_matrix(URL)
    text = qr.qr_text(URL, invert=invert, quiet=quiet)
    lines = text.split("\n")
    size = len(matrix) + 2 * quiet
    assert len(lines) == (size + 1) // 2
    assert all(len(line) == size for line in lines)
    assert set(text) <= set("▀▄█ \n")


def test_text_draws_light_modules_by_default():
    matrix = qr.qr_matrix(URL)
    lines = qr.qr_text(URL).split("\n")
    # The quiet zone is solid. Line 2 holds module rows 0 and 1: the finder's dark top edge
    # (unlit) over its light inner ring (lit) between two dark columns.
    assert lines[0] == "█" * len(lines[0])
    assert matrix[0][0] and not matrix[1][1]
    assert lines[2][3:12] == "█ ▄▄▄▄▄ █"  # quiet zone and separator on either side
    inverted = qr.qr_text(URL, invert=True).split("\n")
    assert inverted[0] == " " * len(inverted[0])
    assert inverted[2][4:11] == "█▀▀▀▀▀█"
