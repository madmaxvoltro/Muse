"""QR codes with only the Python standard library, for ./muse pair and the web setup.

Byte mode, versions 1-40, error correction L/M/Q/H, automatic mask choice. Follows the
structure of Project Nayuki's QR Code generator (MIT licence), reduced to what Muse needs.
Renders to terminal text (half blocks) and to SVG.
"""

from __future__ import annotations

ECC_ORDINAL = {"L": 0, "M": 1, "Q": 2, "H": 3}
ECC_FORMAT_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}

# [ecc][version] tables from ISO/IEC 18004 (index 0 unused)
ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
NUM_ERROR_CORRECTION_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int, ecc: str) -> int:
    e = ECC_ORDINAL[ecc]
    return _raw_modules(ver) // 8 - ECC_CODEWORDS_PER_BLOCK[e][ver] * NUM_ERROR_CORRECTION_BLOCKS[e][ver]


def _rs_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _rs_mul(coef, factor)
    return result


def _alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    num = ver // 7 + 2
    step = (ver * 8 + num * 3 + 5) // (num * 4 - 4) * 2
    result = [ver * 4 + 10 - i * step for i in range(num - 1)] + [6]
    return list(reversed(result))


class QrCode:
    def __init__(self, ver: int, ecc: str, codewords: list[int], mask: int | None = None):
        self.version = ver
        self.size = ver * 4 + 17
        self.ecc = ecc
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.is_function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()
        all_cw = self._add_ecc_and_interleave(codewords)
        self._draw_codewords(all_cw)
        if mask is None:
            best, best_penalty = 0, None
            for m in range(8):
                self._apply_mask(m)
                self._draw_format_bits(m)
                p = self._penalty()
                if best_penalty is None or p < best_penalty:
                    best, best_penalty = m, p
                self._apply_mask(m)  # undo (XOR)
            mask = best
        self.mask = mask
        self._apply_mask(mask)
        self._draw_format_bits(mask)

    # ---- function patterns ----
    def _set_function(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.is_function[y][x] = True

    def _draw_function_patterns(self) -> None:
        for i in range(self.size):
            self._set_function(6, i, i % 2 == 0)
            self._set_function(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (self.size - 4, 3), (3, self.size - 4)):
            self._draw_finder(x, y)
        pos = _alignment_positions(self.version)
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if not ((i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0)):
                    self._draw_alignment(pos[i], pos[j])
        self._draw_format_bits(0)
        self._draw_version()

    def _draw_finder(self, x: int, y: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                xx, yy = x + dx, y + dy
                if 0 <= xx < self.size and 0 <= yy < self.size:
                    dist = max(abs(dx), abs(dy))
                    self._set_function(xx, yy, dist not in (2, 4))

    def _draw_alignment(self, x: int, y: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set_function(x + dx, y + dy, max(abs(dx), abs(dy)) != 1)

    def _draw_format_bits(self, mask: int) -> None:
        data = ECC_FORMAT_BITS[self.ecc] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        get = lambda i: (bits >> i) & 1 != 0  # noqa: E731
        for i in range(6):
            self._set_function(8, i, get(i))
        self._set_function(8, 7, get(6))
        self._set_function(8, 8, get(7))
        self._set_function(7, 8, get(8))
        for i in range(9, 15):
            self._set_function(14 - i, 8, get(i))
        for i in range(8):
            self._set_function(self.size - 1 - i, 8, get(i))
        for i in range(8, 15):
            self._set_function(8, self.size - 15 + i, get(i))
        self._set_function(8, self.size - 8, True)

    def _draw_version(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            bit = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self._set_function(a, b, bit)
            self._set_function(b, a, bit)

    # ---- data ----
    def _add_ecc_and_interleave(self, data: list[int]) -> list[int]:
        e = ECC_ORDINAL[self.ecc]
        num_blocks = NUM_ERROR_CORRECTION_BLOCKS[e][self.version]
        block_ecc_len = ECC_CODEWORDS_PER_BLOCK[e][self.version]
        raw = _raw_modules(self.version) // 8
        num_short = num_blocks - raw % num_blocks
        short_len = raw // num_blocks
        divisor = _rs_divisor(block_ecc_len)
        blocks, k = [], 0
        for i in range(num_blocks):
            dat = data[k: k + short_len - block_ecc_len + (0 if i < num_short else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, divisor)
            if i < num_short:
                dat = dat + [0]
            blocks.append(dat + ecc)
        result = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != short_len - block_ecc_len or j >= num_short:
                    result.append(blk[i])
        return result

    def _draw_codewords(self, data: list[int]) -> None:
        i = 0
        right = self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = self.size - 1 - vert if upward else vert
                    if not self.is_function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    def _apply_mask(self, mask: int) -> None:
        fns = (
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        )
        f = fns[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.is_function[y][x] and f(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self) -> int:
        size, m, result = self.size, self.modules, 0
        for rows in (m, [list(col) for col in zip(*m)]):
            for row in rows:
                run_color, run_len, history = False, 0, [0] * 7
                for x in range(size):
                    if row[x] == run_color:
                        run_len += 1
                        if run_len == 5:
                            result += 3
                        elif run_len > 5:
                            result += 1
                    else:
                        self._history_add(run_len, history, size)
                        if not run_color:
                            result += self._finder_count(history) * 40
                        run_color, run_len = row[x], 1
                result += self._finder_terminate(run_color, run_len, history, size) * 40
        for y in range(size - 1):
            for x in range(size - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    result += 3
        dark = sum(sum(1 for v in row if v) for row in m)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        result += k * 10
        return result

    @staticmethod
    def _history_add(run_len: int, history: list[int], size: int) -> None:
        if history[0] == 0:
            run_len += size
        history.pop()
        history.insert(0, run_len)

    @staticmethod
    def _finder_count(h: list[int]) -> int:
        n = h[1]
        core = n > 0 and h[2] == h[4] == h[5] == n and h[3] == n * 3
        return (1 if core and h[0] >= n * 4 and h[6] >= n else 0) + (1 if core and h[6] >= n * 4 and h[0] >= n else 0)

    def _finder_terminate(self, run_color: bool, run_len: int, history: list[int], size: int) -> int:
        if run_color:
            self._history_add(run_len, history, size)
            run_len = 0
        run_len += size
        self._history_add(run_len, history, size)
        return self._finder_count(history)


def encode(text: str | bytes, ecc: str = "M") -> QrCode:
    data = text.encode("utf-8") if isinstance(text, str) else text
    for ver in range(1, 41):
        cap_bits = _data_codewords(ver, ecc) * 8
        count_bits = 8 if ver <= 9 else 16
        used = 4 + count_bits + len(data) * 8
        if used <= cap_bits:
            break
    else:
        raise ValueError("too much data for a QR code")
    bits: list[int] = []

    def put(val: int, n: int) -> None:
        bits.extend((val >> i) & 1 for i in reversed(range(n)))

    put(0x4, 4)  # byte mode
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap_bits - len(bits)))
    put(0, (8 - len(bits) % 8) % 8)
    pad = 0xEC
    while len(bits) < cap_bits:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i: i + 8])), 2) for i in range(0, len(bits), 8)]
    return QrCode(ver, ecc, codewords)


def to_terminal(qr: QrCode, border: int = 2, invert: bool = False) -> str:
    """Two module rows per text line with half blocks. Dark modules print as spaces on a
    light background by default, which most terminal themes scan well."""
    n = qr.size + border * 2

    def dark(x: int, y: int) -> bool:
        x, y = x - border, y - border
        return 0 <= x < qr.size and 0 <= y < qr.size and qr.modules[y][x]

    lines = []
    for y in range(0, n, 2):
        row = []
        for x in range(n):
            top, bottom = dark(x, y), dark(x, y + 1) if y + 1 < n else False
            if invert:
                top, bottom = not top, not bottom
            # light = printed block; dark = blank (terminal background)
            row.append({(False, False): "█", (True, True): " ", (True, False): "▄", (False, True): "▀"}[(top, bottom)])
        lines.append("".join(row))
    return "\n".join(lines)


def to_svg(qr: QrCode, border: int = 3, dark: str = "currentColor", light: str = "transparent") -> str:
    n = qr.size + border * 2
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y in range(qr.size) for x in range(qr.size) if qr.modules[y][x])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges" role="img">'
            f'<rect width="100%" height="100%" fill="{light}"/><path d="{path}" fill="{dark}"/></svg>')
