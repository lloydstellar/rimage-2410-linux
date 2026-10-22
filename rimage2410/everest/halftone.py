"""Colour pipeline for halftone methods 0 and 2.

Steps: colour matching through a 3D LUT, a 3x3 median filter, halftoning with threshold
matrices and tone curves, and heat compensation for the print head. All steps work line by
line; within a line everything is vectorised with NumPy.

Input of process(): RGB array (h, 1889, 3), already mirrored.
Output: {plane: uint8 (h, 1889)}; color.py pads the lines to 1920 and adds the stamp.
"""

from __future__ import annotations

import numpy as np

from .color import table

W_MAX = 0x762
M32 = 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

def make_flags(ribbon: str, colormatch: int = 1, alt: bool = False, heat: bool = True,
               median: bool = True, k_slot: int = 5) -> int:
    """Flag word for the conversion.

    Low nibble: colour match set (colour ribbons) or K slot (mono). 0x10: alternative table set.
    0x100: no heat compensation. 0x800: no median filter."""
    if ribbon == "mono":
        if k_slot not in (0, 4, 5):
            raise ValueError("K slot must be 0, 4 or 5")
        flags = k_slot
    else:
        if colormatch not in (0, 1, 2):
            raise ValueError("colormatch must be 0, 1 or 2")
        flags = colormatch | (0x10 if alt else 0)
    if not heat:
        flags |= 0x100
    if not median:
        flags |= 0x800
    return flags


# ---------------------------------------------------------------------------
# Colour matching: 3D LUT with interpolation, vectorised over the whole image
# ---------------------------------------------------------------------------

def _lut_name(sel: int, method: int) -> str:
    mode = sel & 0xF
    alt = "_flag10" if sel & 0x10 else ""
    return f"lut3d_mode{mode}{alt}_method{method}"


def colormatch(bgr: np.ndarray, sel: int, method: int) -> np.ndarray:
    """bgr: uint8 (..., 3) in B, G, R order. Returns a new array."""
    lut = table(_lut_name(sel, method)).astype(np.int64)
    axis = table("lut_axis_u32x256", "<u4").astype(np.int64)
    wts = table("lut_weight_u16x4096", "<u2").astype(np.int64)
    shape = bgr.shape
    px = bgr.reshape(-1, 3).astype(np.int64)
    e = [axis[px[:, k]] for k in range(3)]                 # e0 belongs to byte 0 (B)
    lo = [ek & 0xFF for ek in e]
    hi = [(ek >> 8) & 0xFF for ek in e]
    fu = [(ek >> 16) & 0xFF for ek in e]
    fl = [ek >> 24 for ek in e]
    on = [lo[k] == hi[k] for k in range(3)]
    base = (((lo[2] * 17 + lo[1]) * 17 + lo[0]) * 3) & 0xFFFF
    acc = np.zeros((px.shape[0], 3), dtype=np.int64)
    for c2 in (0, 1):
        for c1 in (0, 1):
            for c0 in (0, 1):
                use = np.ones(px.shape[0], dtype=bool)
                if c0:
                    use &= ~on[0]
                if c1:
                    use &= ~on[1]
                if c2:
                    use &= ~on[2]
                i0 = fu[0] if c0 else fl[0]
                i1 = fu[1] if c1 else fl[1]
                i2 = fu[2] if c2 else fl[2]
                wt = wts[(((i2 * 16 + i1) * 16 + i0) & 0xFFFF)]
                off = base + c0 * 3 + c1 * 0x33 + c2 * 0x363
                for k in range(3):
                    acc[:, k] += np.where(use, lut[np.clip(off + k, 0, len(lut) - 1)] * wt, 0)
    ncount = on[0].astype(np.int64) + on[1] + on[2]
    shift = np.select([ncount == 0, ncount == 1, ncount == 2], [12, 8, 4], 0)
    out = (acc >> shift[:, None]) & 0xFF
    exact = ncount == 3
    if exact.any():
        b = base[exact]
        out[exact] = np.stack([lut[b], lut[b + 1], lut[b + 2]], axis=1)
    return out.astype(np.uint8).reshape(shape)


# ---------------------------------------------------------------------------
# 3x3 median filter, only for 15 < c < 240
# ---------------------------------------------------------------------------

def median_filter(rows: np.ndarray) -> np.ndarray:
    """rows: uint8 (n+2, w, 3), including the neighbour line above and below. Returns (n, w, 3)."""
    n = rows.shape[0] - 2
    padded = np.concatenate([rows[:, :1], rows, rows[:, -1:]], axis=1)   # repeat edge column
    stack = []
    for dy in range(3):
        for dx in range(3):
            stack.append(padded[dy:dy + n, dx:dx + rows.shape[1]])
    med = np.median(np.stack(stack, axis=0), axis=0).astype(np.uint8)
    center = rows[1:n + 1]
    apply = (center > 15) & (center < 240)
    return np.where(apply, med, center)


# ---------------------------------------------------------------------------
# Halftoning (colour and K)
# ---------------------------------------------------------------------------

class Screen:
    """Halftone state of one channel (one slot), with its pair of line buffers."""

    def __init__(self, slot: int, w: int):
        rows = int(table("scr_rows_u16x36", "<u2")[slot])
        cols = int(table("scr_cols_u16x36", "<u2")[slot])
        self.N = int(table("scr_N_i32x36", "<i4")[slot])
        if self.N == 0:
            raise ValueError(f"screen slot {slot} is not in use")
        step = int(table("scr_rampstep_u8x36")[slot])
        self.ramp = np.minimum(np.arange(256) * step, 255).astype(np.int64)
        m = table(f"matrix_{slot:02d}", "<u2")[: rows * cols].reshape(rows, cols).astype(np.int64)
        reps = -(-w // cols)
        self.matrix = np.tile(m, (1, reps))[:, :w]
        self.rows = rows
        self.curve = table(f"curve_{slot:02d}", "<u2").astype(np.int64)
        self.levels = table(f"levels_{slot:02d}", "<u4").astype(np.int64)
        self.stride = table(f"stride_{slot:02d}", "<u2").astype(np.int64)
        self.grid = table(f"grid_{slot:02d}").astype(np.int64)
        self.full = table(f"full_{slot:02d}").astype(np.int64)
        self.floor = table(f"floor_{slot:02d}").astype(np.int64)
        self.spread = table(f"spread_{slot:02d}").astype(np.int64)
        self.buf = [np.zeros(w, dtype=np.int64), np.zeros(w, dtype=np.int64)]

    def step(self, i: int, v: np.ndarray, c: np.ndarray) -> np.ndarray:
        """Process image line i (v = ink 0..255, c = tone index) and return line i-1."""
        cur, prev = self.buf[i & 1], self.buf[1 - (i & 1)]
        t = self.matrix[i % self.rows]
        th, tl = t >> 8, t & 0xFF
        full = v == 255

        nz_c = c != 0
        cm1 = np.where(nz_c, c - 1, 0)
        q, r = cm1 // self.N, cm1 % self.N
        e = self.levels[np.clip(q & 0xFFFF, 0, len(self.levels) - 1)]
        e0, e1, e2, e3 = e & 0xFF, (e >> 8) & 0xFF, (e >> 16) & 0xFF, e >> 24
        thr = (r + e0 * self.N) & 0xFFFF
        lvl = np.where(nz_c, e2, 0)

        grid_idx = np.clip((e2 - 1) + (th - 1) * self.stride[0], 0, len(self.grid) - 1)
        out_lt = self.grid[grid_idx]
        sth = self.stride[np.clip(th, 0, len(self.stride) - 1)] & 0xFF
        idx = np.where(tl <= thr, e1 + 1, np.where(sth == e1, e3, e1))
        out_eq = self.ramp[np.clip(idx & 0xFF, 0, 255)]
        out = np.where(th < e2, out_lt, np.where(th == e2, out_eq, 0))
        out = np.where(nz_c, out, 0)

        s = self.spread[lvl]
        s_full = self.spread[self.stride[0]]
        v_prev = np.concatenate([[0], v[:-1]])
        x_gt0 = np.arange(len(v)) > 0

        # full ink
        out = np.where(full, self.full[th], out)
        s = np.where(full, s_full, s)
        # output 0: floor value, or the spread of a level-0 neighbour
        zero = ~full & (out == 0)
        floor_val = np.where(x_gt0 & (v_prev != 0) & (lvl == 0), s, self.floor[lvl])
        nz_out = ~full & (out != 0)
        out = np.where(zero, floor_val, out)

        # raise the previous line
        raise_prev = full | nz_out
        new_prev = np.where(raise_prev, np.maximum(prev, s), prev)

        # current line: pixel x adjusts cur[x-1]
        new_cur = out.copy()
        nxt_full = full[1:]
        nxt_nz = nz_out[1:] & (v[:-1] == 0)
        left = new_cur[:-1]
        left = np.where(nxt_full, np.maximum(left, s[1:]), left)
        left = np.where(nxt_nz & ~nxt_full, s[1:], left)
        new_cur[:-1] = left

        self.buf[i & 1] = new_cur
        self.buf[1 - (i & 1)] = new_prev
        return new_prev.astype(np.uint8)


def color_tones(v1, v2, v3, v4, curves):
    """Tone index per channel from the tone curves. M also depends on C, Y on C and M."""
    cC, cM, cY = curves[1], curves[2], curves[3]
    c1 = cC[v1]
    c2 = cM[v2]
    d2 = cM[256 + v2]
    c2 = np.where(d2 != 0, (np.trunc(((d2 - c2) * v1 + 0x7F) / 0xFF).astype(np.int64) + c2) & 0xFFFF, c2)
    c3 = cY[v3]
    d3 = cY[256 + v3]
    num = (0xFE01 - (255 - v1) * (255 - v2)) * (d3 - c3) + 0x7F00
    c3 = np.where(d3 != 0, (np.trunc(num / 0xFE01).astype(np.int64) + c3) & 0xFFFF, c3)
    tones = {1: c1, 2: c2, 3: c3}
    if v4 is not None:
        tones[4] = curves[4][v4]
    return tones


# ---------------------------------------------------------------------------
# Heat compensation: per-pixel and per-block heat history of the print head
# ---------------------------------------------------------------------------

class Heat:
    """Heat history and parameters for one plane p (1..4 = C, M, Y, W; 0 or 5 = K)."""

    def __init__(self, p: int, w: int):
        q = 4 if p == 5 else p
        self.p = p
        self.w = w
        self.mode = int(table("heat_mode_i32x5", "<i4")[q])
        self.P = [int(x) for x in table("heat_params_5x23", "<u4").reshape(5, 23)[q]]
        self.enable = int(table("heat_energy_enable_i32x5", "<i4")[q])
        self.eshift = int(table("heat_energy_shift_i32x5", "<i4")[q])
        self.pshift = int(table("heat_profile_shift_i32x5", "<i4")[q])
        self.inlut = table("heat_inlut_00", "<u4").astype(np.int64)
        self.loadscale = table("heat_loadscale_00", "<u4").astype(np.int64)
        self.profile = table("heat_profile_00", "<u4").astype(np.int64)[:w]
        self.round = table("round_4x4_u32", "<u4").astype(np.int64)
        self.H = np.zeros(w, dtype=np.int64)
        self.B = np.zeros(0xED, dtype=np.int64)
        # edge pattern
        self.edge_count = table("edge_count_u16", "<u2").astype(np.int64)
        self.edge_pattern = table("edge_pattern_u8").reshape(6, 8, 8)
        dflt_tab = table("edge_default_u8")
        k = {1: 4, 2: 8, 3: 12, 5: 16}.get(p, 0)
        dv = int(dflt_tab[k])
        self.edge_default = 0 if dv < 0xB else dv - 10

    def edge_fill(self, src: np.ndarray, line: int) -> None:
        """Fill the area outside the round disc with a pattern. src is 0-based, length w."""
        d = 0x762 - line if line > 0x3B0 else line
        d = min(max(d, 0), 0x3B0)
        count = int(self.edge_count[d])
        if count == 0:
            return
        pat = self.edge_pattern[self.p if self.p < 6 else 0][line & 7]
        i = np.arange(count)
        left = pat[i % 8]
        lmask = i < self.w
        src[i[lmask]] = left[lmask]
        right_x = 0x762 - i
        k = (2 - i) % 8
        right = np.where((i > 0x55) | (line > 0x3B0), pat[k], self.edge_default)
        rmask = (right_x >= 0) & (right_x < self.w)
        src[right_x[rmask]] = right[rmask]

    def _run(self, L: int, IN: np.ndarray) -> np.ndarray:
        P, w = self.P, self.w
        bidx = np.minimum(np.arange(w) >> 3, len(self.B) - 1)
        o = IN - (((self.B[bidx] * P[7]) & M32) >> P[8]) - (((P[5] * self.H) & M32) >> P[6])
        o = np.where(o < P[22], P[22], np.where(o > P[21], P[21], o))
        R = o.copy()
        if self.enable:
            E = int(o[o > 0].sum()) // P[21]
            E = min(E, w)
            s = int(self.loadscale[E])
            R = ((R * s) & M32) >> self.eshift
            R = ((self.profile * R) & M32) >> self.pshift
            R = np.minimum(R, P[21])
        q = R >> 4
        thr = self.round[(L & 3) * 4 + (np.arange(w) & 3)]
        q = np.where(thr < (R & 0xF), q + 1, q)
        R = np.minimum(q, 255)
        # pixel history
        Hn = self.H + (o >> P[9])
        Hn = np.where(Hn > P[0], P[0], Hn)
        if w > 2:
            lap = 2 * Hn[1:-1] - Hn[2:] - Hn[:-2]
            neg = lap < 0
            up = (((-(P[12] * lap)) & M32) >> P[13]) + Hn[1:-1]
            down = Hn[1:-1] - (((P[12] * lap) & M32) >> P[13])
            mid = np.where(neg, up, down)
            mid = np.where(mid > P[0], P[0], mid)
            mid = np.where(mid < 0, 0, mid)
            Hn[1:-1] = mid
        Hn = ((Hn * P[14]) & M32) >> P[15]
        self.H = np.where(Hn > P[0], P[0], Hn)
        # block history
        nb = w >> 3
        Bn = self.B.copy()
        sums = o[: nb * 8].reshape(nb, 8).sum(axis=1)
        Bn[:nb] = Bn[:nb] + (sums >> P[16])
        Bn[:nb] = np.where(Bn[:nb] > P[1], P[1], Bn[:nb])
        if nb - 1 > 1:
            orig = Bn[:nb].copy()
            lap = 2 * orig[1:-1] - orig[2:] - orig[:-2]
            neg = lap < 0
            d_neg = ((-(P[17] * lap)) & M32) >> P[18]
            d_pos = -((((P[17] * lap) & M32) >> P[18]))
            new = np.where(neg, d_neg, d_pos) + orig[1:-1]
            new = ((new + 2**31) & M32) - 2**31          # signed 32-bit
            new = np.where(new <= P[1], np.where(new < 0, 0, new), P[1])
            Bn[1:nb - 1] = new
        Bn[:nb] = ((P[19] * Bn[:nb]) & M32) >> P[20]
        Bn[:nb] = np.where(Bn[:nb] > P[1], P[1], Bn[:nb])
        self.B = Bn
        return R

    def apply(self, src: np.ndarray, line: int) -> np.ndarray:
        src = src.astype(np.int64).copy()
        self.edge_fill(src, line)
        IN_all = self.inlut[src]
        if self.mode != 2:
            return self._run(line, IN_all).astype(np.uint8)
        out = np.zeros(self.w, dtype=np.int64)
        x = np.arange(self.w)
        for half in (0, 1):
            sel = (x % 2) == half
            IN = np.where(sel, IN_all, 0)
            R = self._run(half + 2 * line, IN)
            out[sel] = R[sel]
        return out.astype(np.uint8)


# ---------------------------------------------------------------------------
# The full pipeline
# ---------------------------------------------------------------------------

def process(rgb: np.ndarray, *, ribbon: str, method: int, flags: int,
            white_mask: np.ndarray | None = None, progress=None) -> dict[int, np.ndarray]:
    h, w, _ = rgb.shape
    if method == 0 and (flags & 0x10) and ribbon != "mono":
        raise ValueError("method 0 with the alternative table set gives no output (N = 0)")
    bgr = rgb[..., ::-1].copy()                         # B, G, R order
    white_row = np.full((1, w, 3), 255, dtype=np.uint8)

    # colour matching, also for the two white lines appended after the image
    tail = np.concatenate([white_row, white_row], axis=0)
    work = np.concatenate([bgr, tail], axis=0)
    mono = ribbon == "mono"
    sel = flags & 0xF
    if not mono and not flags & 0x400 and sel in (1, 2):
        work = colormatch(work, sel | (flags & 0x10), method)

    # median filter: one white line (0xFF) above, the appended lines below
    rows = np.concatenate([white_row, work], axis=0)
    if flags & 0x800:
        filt = work[: h + 1]
    else:
        filt = median_filter(rows[: h + 3])             # lines 0..h
    # filt[i] for i = 0..h; i = h is the first appended white line

    has_w = ribbon == "cmyw"
    if has_w:
        if white_mask is None:
            wm = np.all(rgb == 255, axis=2).astype(np.uint8) * 255
        else:
            wm = np.asarray(white_mask, dtype=np.uint8)
        wm = np.concatenate([wm, wm[-1:]], axis=0)      # appended line keeps the last mask line

    if mono:
        slot = (flags & 0xFF) + method * 9
        screens = {0: Screen(slot, w)}
        heat_planes = {0: Heat(flags & 0xFF, w)}
        lr, lg, lb = (table("lum_r_u16x256", "<u2").astype(np.int64),
                      table("lum_g_u16x256", "<u2").astype(np.int64),
                      table("lum_b_u16x256", "<u2").astype(np.int64))
        curve_k = screens[0].curve
    else:
        base = method * 9 + (5 if flags & 0x10 else 0)
        chans = (1, 2, 3, 4) if has_w else (1, 2, 3)
        screens = {ch: Screen(base + ch, w) for ch in chans}
        heat_planes = {ch: Heat(ch, w) for ch in chans}
        curves = {ch: screens[ch].curve for ch in chans}

    out = {p: np.zeros((h, w), dtype=np.uint8) for p in screens}
    for i in range(h + 1):
        line = filt[i].astype(np.int64)
        b, g, r = line[:, 0], line[:, 1], line[:, 2]
        if mono:
            gray = (lr[r] + lg[g] + lb[b]) & 0xFFFF
            v = 255 - ((gray >> 8) & 0xFF)
            scr = {0: screens[0].step(i, v, curve_k[v])}
        else:
            v1, v2, v3 = 255 - r, 255 - g, 255 - b
            v4 = None
            if has_w:
                v4 = 255 - wm[i].astype(np.int64)
                ink = v4 != 0
                v3 = np.where(ink & (v3 == 0), 1, v3)
                v2 = np.where(ink & (v2 == 255), 254, v2)
                v1 = np.where(ink & (v1 == 255), 254, v1)
            tones = color_tones(v1, v2, v3, v4, curves)
            vals = {1: v1, 2: v2, 3: v3, 4: v4}
            scr = {ch: screens[ch].step(i, vals[ch], tones[ch]) for ch in screens}
        if i == 0:
            continue                                    # no finished line yet
        row = i - 1
        for p, s in scr.items():
            if flags & 0x100:
                out[p][row] = s
            else:
                out[p][row] = heat_planes[p].apply(s, row)
        if progress and row % 64 == 0:
            progress(row, h)
    return out
