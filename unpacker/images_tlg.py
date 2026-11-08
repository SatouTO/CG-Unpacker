"""图片解码器：TLG5 / TLG6（KiriKiri 引擎）

参考 GARbro 的 ArcFormats/KiriKiri/ImageTLG.cs（TVP 原始解码器的 C# 移植）。
解码出来的像素本来就是一像素 4 字节的 BGRA，和 images.py 里的 QNT/AJP 一样，
直接丢给 Pillow 就行。
"""
import struct

from PIL import Image

from .registry import register_image

# TVP_TLG6 的块尺寸，规范写死的，别改
_TLG6_W_BLOCK = 8
_TLG6_H_BLOCK = 8


def _i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


# ---------------------------------------------------------------- 查表
def _make_lz_table():
    """TVPTLG6LeadingZeroTable：最低置位的位置 + 1，全 0 给 0"""
    tab = bytearray(4096)
    for i in range(4096):
        cnt = 0
        j = 1
        while j != 4096 and not (i & j):
            j <<= 1
            cnt += 1
        cnt += 1
        tab[i] = 0 if j == 4096 else cnt
    return bytes(tab)


# TVPTLG6GolombCompressed：每个 n 下 k 值的分布（W.Dee 2004/03/25 调过）
_GOLOMB_COMPRESSED = (
    (3, 7, 15, 27, 63, 108, 223, 448, 130),
    (3, 5, 13, 24, 51, 95, 192, 384, 257),
    (2, 5, 12, 21, 39, 86, 155, 320, 384),
    (2, 3, 9, 18, 33, 61, 129, 258, 511),
)


def _make_golomb_table():
    """按 (a << 2) | n 展平成一张 4096 的表，a 是前几个误差绝对值之和"""
    tab = bytearray(4096)
    for n, counts in enumerate(_GOLOMB_COMPRESSED):
        a = 0
        for k, cnt in enumerate(counts):
            for _ in range(cnt):
                tab[(a << 2) | n] = k
                a += 1
    return bytes(tab)


def _make_lzss_seed():
    """TLG6 的 filter type 表是用 TLG5 的 LZSS 压的，初始文本窗是个固定花样"""
    out = bytearray(4096)
    p = 0
    for i in range(32):
        for j in range(16):
            out[p:p + 8] = struct.pack("<II", i, j)
            p += 8
    return bytes(out)


_LZ_TAB = _make_lz_table()
_GOLOMB_K = _make_golomb_table()
_LZSS_SEED = _make_lzss_seed()


# ---------------------------------------------------------------- 头部
def tlg_meta(d: bytes):
    """解析 TLG 头，返回 (version, colors, width, height, 数据偏移)，认不出返回 None"""
    if len(d) < 0x26:
        return None
    hd = bytearray(d[:0x26])            # 头部可能被异或，拷一份改
    off = 0
    if hd[:11] == b"TLG0.0\x00sds\x1a":  # KiriKiri 的 "sds" 包装
        off = 0xF
    if bytes(hd[off + 6:off + 11]) != b"\x00raw\x1a":
        return None
    if hd[off] == 0xAB:                  # 首字节被取反的 TLG5/TLG6
        hd[off] = 0x54                   # 'T'
    sig = bytes(hd[off:off + 6])
    if sig == b"TLG6.0":
        ver = 6
    elif sig == b"TLG5.0":
        ver = 5
    elif sig == b"XXXYYY":               # 宽高首字节加密的 TLG5
        ver = 5
        hd[off + 0x0C] ^= 0xAB
        hd[off + 0x10] ^= 0xAC
    elif sig == b"XXXZZZ":               # 宽高首字节加密的 TLG6
        ver = 6
        hd[off + 0x0F] ^= 0xAB
        hd[off + 0x13] ^= 0xAC
    elif sig == b"JKMXE8":               # 另一种 TLG5 变体
        ver = 5
        hd[off + 0x0C] ^= 0x1A
        hd[off + 0x10] ^= 0x1C
    else:
        return None
    colors = hd[off + 11]
    if ver == 6:
        if colors not in (1, 3, 4) or hd[off + 12] or hd[off + 13] or hd[off + 14]:
            return None
        off += 15
    else:
        if colors not in (3, 4):
            return None
        off += 12
    width, height = struct.unpack_from("<II", hd, off)
    if not (0 < width <= 20000 and 0 < height <= 20000):
        return None
    if width * height > (1 << 26):       # 坏头别把内存吃光（正常游戏图远到不了）
        return None
    return ver, colors, width, height, off + 8


# ---------------------------------------------------------------- LZSS
def _lzss_decompress(src: bytes, out_size: int, text: bytearray, r: int):
    """TVP 的 LZSS（带 4096 的滑动文本窗），返回 (输出, 新的文本窗位置)

    TLG5 的像素块和 TLG6 的 filter type 表都用它压。
    """
    out = bytearray(out_size)
    o = 0
    i = 0
    n = len(src)
    flags = 0
    while i < n:
        flags >>= 1
        if not (flags & 0x100):
            flags = src[i] | 0xFF00
            i += 1
        if flags & 1:
            if i + 1 >= n:
                break
            mpos = src[i] | ((src[i + 1] & 0x0F) << 8)
            mlen = (src[i + 1] >> 4) + 3
            i += 2
            if mlen == 18:               # 15 是转义，后面再跟一个长度字节
                if i >= n:
                    break
                mlen += src[i]
                i += 1
            if o + mlen > out_size:
                raise ValueError("LZSS 输出溢出")
            for _ in range(mlen):
                c = text[mpos & 4095]
                text[r & 4095] = c
                out[o] = c
                o += 1
                mpos += 1
                r += 1
        else:
            if o >= out_size or i >= n:
                raise ValueError("LZSS 输出溢出")
            c = src[i]
            i += 1
            out[o] = c
            o += 1
            text[r & 4095] = c
            r += 1
    return out, r & 4095


# ---------------------------------------------------------------- TLG5
def _tlg5(d: bytes, pos: int, width: int, height: int, colors: int) -> bytes:
    n = len(d)
    if pos + 4 > n:
        raise ValueError("TLG5 头被截断")
    blockheight = _i32(d, pos)
    pos += 4
    if blockheight <= 0:
        raise ValueError("TLG5 blockheight 非法")
    if blockheight > height:
        blockheight = height
    blockcount = (height - 1) // blockheight + 1
    pos += blockcount * 4                # 每块的压缩长度表，解码时用不上
    if pos > n:
        raise ValueError("TLG5 块表被截断")

    text = bytearray(4096)               # TLG5 的文本窗从全 0 开始
    z = 0                                # 文本窗写入位置，跨块跨通道延续
    stride = width * 4
    img = bytearray(height * stride)

    for y_blk in range(0, height, blockheight):
        bufs = []
        for c in range(colors):
            if pos + 5 > n:
                raise ValueError("TLG5 数据被截断")
            mark = d[pos]
            size = _i32(d, pos + 1)
            pos += 5
            if size < 0 or pos + size > n:
                raise ValueError("TLG5 块大小非法")
            if mark == 0:                # 0 = LZSS，其它 = 原样
                buf, z = _lzss_decompress(d[pos:pos + size],
                                          blockheight * width + 10, text, z)
            else:
                buf = d[pos:pos + size]
            pos += size
            bufs.append(buf)

        y_lim = y_blk + blockheight
        if y_lim > height:
            y_lim = height
        o = 0                            # 块内行偏移
        for y in range(y_blk, y_lim):
            cur = y * stride
            if y == 0:                   # 整张图的第一行，没有上一行
                if colors == 3:
                    b0, b1, b2 = bufs
                    pb0 = pg0 = pr0 = 0
                    p = 0
                    for _ in range(width):
                        g = b1[p]
                        bb = b0[p] + g
                        rr = b2[p] + g
                        pb0 = (pb0 + bb) & 0xFF
                        pg0 = (pg0 + g) & 0xFF
                        pr0 = (pr0 + rr) & 0xFF
                        img[cur] = pb0
                        img[cur + 1] = pg0
                        img[cur + 2] = pr0
                        img[cur + 3] = 0xFF
                        cur += 4
                        p += 1
                else:
                    b0, b1, b2, b3 = bufs
                    pb0 = pg0 = pr0 = pa0 = 0
                    p = 0
                    for _ in range(width):
                        g = b1[p]
                        bb = b0[p] + g
                        rr = b2[p] + g
                        pb0 = (pb0 + bb) & 0xFF
                        pg0 = (pg0 + g) & 0xFF
                        pr0 = (pr0 + rr) & 0xFF
                        pa0 = (pa0 + b3[p]) & 0xFF
                        img[cur] = pb0
                        img[cur + 1] = pg0
                        img[cur + 2] = pr0
                        img[cur + 3] = pa0
                        cur += 4
                        p += 1
            else:                        # 其余行：本行差分前缀和 + 上一行
                up = cur - stride
                if colors == 3:
                    b0, b1, b2 = bufs
                    pc0 = pc1 = pc2 = 0
                    p = o
                    for _ in range(width):
                        g = b1[p]
                        bb = b0[p] + g
                        rr = b2[p] + g
                        pc0 = (pc0 + bb) & 0xFF
                        pc1 = (pc1 + g) & 0xFF
                        pc2 = (pc2 + rr) & 0xFF
                        img[cur] = (pc0 + img[up]) & 0xFF
                        img[cur + 1] = (pc1 + img[up + 1]) & 0xFF
                        img[cur + 2] = (pc2 + img[up + 2]) & 0xFF
                        img[cur + 3] = 0xFF
                        cur += 4
                        up += 4
                        p += 1
                else:
                    b0, b1, b2, b3 = bufs
                    pc0 = pc1 = pc2 = pc3 = 0
                    p = o
                    for _ in range(width):
                        g = b1[p]
                        bb = b0[p] + g
                        rr = b2[p] + g
                        pc0 = (pc0 + bb) & 0xFF
                        pc1 = (pc1 + g) & 0xFF
                        pc2 = (pc2 + rr) & 0xFF
                        pc3 = (pc3 + b3[p]) & 0xFF
                        img[cur] = (pc0 + img[up]) & 0xFF
                        img[cur + 1] = (pc1 + img[up + 1]) & 0xFF
                        img[cur + 2] = (pc2 + img[up + 2]) & 0xFF
                        img[cur + 3] = (pc3 + img[up + 3]) & 0xFF
                        cur += 4
                        up += 4
                        p += 1
            o += width
    return bytes(img)


# ---------------------------------------------------------------- TLG6 Golomb
def _golomb_decode(pool: bytes, limit: int, count: int, out: list, offset: int):
    """TLG6 的 Golomb 解码，结果写进 out 里每个元素的第 offset 个字节

    pool 末尾补了 8 个 0（C# 也是这么读越界的），limit 是真实字节数，
    超过就说明位流坏了，抛异常而不是死循环。
    """
    i = 0
    bp = 1                               # bit_pool 的第 0 位是“首段是不是 0”
    zero = not (pool[0] & 1)
    g = 3
    a = 0
    pixel = 0
    lz = _LZ_TAB
    gk = _GOLOMB_K
    mask = ~(0xFF << offset) & 0xFFFFFFFF
    while pixel < count:
        word = pool[i] | (pool[i + 1] << 8) | (pool[i + 2] << 16) | (pool[i + 3] << 24)
        t = word >> bp
        b = lz[t & 0xFFF]
        bc = b
        while not b:                     # 12 位一段找第一个 1
            bc += 12
            bp += 12
            i += bp >> 3
            bp &= 7
            if i > limit:
                raise ValueError("TLG6 位流越界")
            word = pool[i] | (pool[i + 1] << 8) | (pool[i + 2] << 16) | (pool[i + 3] << 24)
            t = word >> bp
            b = lz[t & 0xFFF]
            bc += b
        bp += b
        i += bp >> 3
        bp &= 7
        bc -= 1                          # 这一段游程的长度编码
        word = pool[i] | (pool[i + 1] << 8) | (pool[i + 2] << 16) | (pool[i + 3] << 24)
        cnt = (1 << bc) + ((word >> bp) & ((1 << bc) - 1))
        bp += bc
        i += bp >> 3
        bp &= 7
        if zero:                         # 全是 0，缓冲区本来就是 0，跳过
            pixel += cnt
            zero = False
            continue
        zero = True
        end = pixel + cnt
        if end > count:
            end = count
        while pixel < end:
            k = gk[(a << 2) | g] if a < 1024 else 255
            word = pool[i] | (pool[i + 1] << 8) | (pool[i + 2] << 16) | (pool[i + 3] << 24)
            t = word >> bp
            if t:
                b = lz[t & 0xFFF]
                bc = b
                while not b:
                    bc += 12
                    bp += 12
                    i += bp >> 3
                    bp &= 7
                    if i > limit:
                        raise ValueError("TLG6 位流越界")
                    word = pool[i] | (pool[i + 1] << 8) | (pool[i + 2] << 16) | (pool[i + 3] << 24)
                    t = word >> bp
                    b = lz[t & 0xFFF]
                    bc += b
                bc -= 1
            else:                        # 32 位窗口全 0 的转义：长度塞在整字节里
                i += 5
                if i > limit:
                    raise ValueError("TLG6 位流越界")
                bc = pool[i - 1]
                bp = 0
                word = pool[i] | (pool[i + 1] << 8) | (pool[i + 2] << 16) | (pool[i + 3] << 24)
                t = word
                b = 0
            e = (bc << k) + ((t >> b) & ((1 << k) - 1))
            sign = (e & 1) - 1
            e >>= 1
            a += e
            out[pixel] = (out[pixel] & mask) | ((((e ^ sign) + sign + 1) & 0xFF) << offset)
            pixel += 1
            bp += b + k
            i += bp >> 3
            bp &= 7
            g -= 1
            if g < 0:
                a >>= 1
                g = 3


# ---------------------------------------------------------------- TLG6 色度变换
def _tlg6_transform(pbuf: list, rows: int, xbc: int, mcount: int, frac: int,
                    ftypes: bytes, ftbase: int):
    """按 filter type 把“通道差分”反相关成 BGRA 差分（16 种编解码器约定的花样）

    相邻块列是蛇形排列的，所以奇数块列要按行倒过来取。
    """
    for i in range(xbc):
        t = ftypes[ftbase + i] >> 1
        if t == 0:                       # 最常见：原样
            continue
        if i < mcount:
            bw = _TLG6_W_BLOCK
            base = rows * _TLG6_W_BLOCK * i
        else:
            bw = frac
            base = rows * _TLG6_W_BLOCK * mcount
        rev = i & 1
        for r in range(rows):
            off = base + bw * (rows - 1 - r if rev else r)
            end = off + bw
            if t == 1:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | ((b + g) & 0xFF)
                               | (g << 8) | (((rr + g) & 0xFF) << 16))
            elif t == 2:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (b & 0xFF)
                               | (((g + b) & 0xFF) << 8) | (((rr + g + b) & 0xFF) << 16))
            elif t == 3:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (((b + g + rr) & 0xFF))
                               | (((g + rr) & 0xFF) << 8) | ((rr & 0xFF) << 16))
            elif t == 4:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (((b + rr) & 0xFF))
                               | (((g + b + rr) & 0xFF) << 8)
                               | (((rr + rr + b + g) & 0xFF) << 16))
            elif t == 5:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (((b + rr) & 0xFF))
                               | (((g + b + rr) & 0xFF) << 8) | ((rr & 0xFF) << 16))
            elif t == 6:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (((b + g) & 0xFF))
                               | (g << 8) | ((rr & 0xFF) << 16))
            elif t == 7:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (b & 0xFF)
                               | (((g + b) & 0xFF) << 8) | ((rr & 0xFF) << 16))
            elif t == 8:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (b & 0xFF)
                               | (g << 8) | (((rr + g) & 0xFF) << 16))
            elif t == 9:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (((b + b + g + rr) & 0xFF))
                               | (((g + rr + b) & 0xFF) << 8) | (((rr + b) & 0xFF) << 16))
            elif t == 10:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (((b + rr) & 0xFF))
                               | (((g + rr) & 0xFF) << 8) | ((rr & 0xFF) << 16))
            elif t == 11:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (b & 0xFF)
                               | (((g + b) & 0xFF) << 8) | (((rr + b) & 0xFF) << 16))
            elif t == 12:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (b & 0xFF)
                               | (((g + rr + b) & 0xFF) << 8) | (((rr + b) & 0xFF) << 16))
            elif t == 13:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (((b + g) & 0xFF))
                               | (((g + g + rr + b) & 0xFF) << 8)
                               | (((rr + b + g) & 0xFF) << 16))
            elif t == 14:
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (((b + g + rr) & 0xFF))
                               | (((g + rr) & 0xFF) << 8)
                               | (((rr + rr + b + g) & 0xFF) << 16))
            else:                        # t == 15
                for j in range(off, end):
                    v = pbuf[j]
                    b = v & 0xFF
                    g = (v >> 8) & 0xFF
                    rr = (v >> 16) & 0xFF
                    pbuf[j] = ((v & 0xFF000000) | (b & 0xFF)
                               | (((g + b + b) & 0xFF) << 8) | (((rr + b + b) & 0xFF) << 16))


# ---------------------------------------------------------------- TLG6 逐行还原
def _tlg6_line(prev: list, cur: list, width: int, bw: int,
               start_block: int, block_limit: int, ftypes: bytes, ftbase: int,
               skipblockbytes: int, pbuf: list, istart: int, initialp: int,
               oddskip: int, dirbit: int):
    """解一个块列区间的一行：MED / AVG 预测 + 差分累加

    C# 里是 TVPTLG6DecodeLineGeneric，所有运算都得按 32 位无符号截断。
    """
    if start_block:
        pi = ci = start_block * _TLG6_W_BLOCK     # 接上左边一个块的最后一个像素
        p = cur[ci - 1]
        up = prev[pi - 1]
    else:
        pi = ci = 0
        p = up = initialp
    ib = istart + skipblockbytes * start_block
    step = 1 if (dirbit & 1) else -1

    for i in range(start_block, block_limit):
        npx = width - i * _TLG6_W_BLOCK
        if npx > bw:
            npx = bw
        if step < 0:
            ib += npx - 1
        if i & 1:
            ib += oddskip * npx

        if ftypes[ftbase + i] & 1:       # AVG 预测
            for _ in range(npx):
                u = prev[pi]
                v = pbuf[ib]
                x = p ^ u
                ap = ((p & u) + ((x & 0xFEFEFEFE) >> 1) + (x & 0x01010101)) & 0xFFFFFFFF
                c2 = (((ap & v) << 1) + ((ap ^ v) & 0xFEFEFEFE)) & 0x01010100
                p = (ap + v - c2) & 0xFFFFFFFF
                cur[ci] = p
                pi += 1
                ci += 1
                ib += step
                up = u
        else:                            # MED（中值边缘检测）预测
            for _ in range(npx):
                u = prev[pi]
                v = pbuf[ib]
                # tvp_make_gt_mask(p, u) -> aa_gt_bb，顺便拆出 min/max
                t2 = u ^ 0xFFFFFFFF
                g1 = ((p & t2) + (((p ^ t2) >> 1) & 0x7F7F7F7F)) & 0x80808080
                aa_gt_bb = ((g1 >> 7) + 0x7F7F7F7F) ^ 0x7F7F7F7F
                t3 = (p ^ u) & aa_gt_bb
                aa = t3 ^ p
                bb = t3 ^ u
                # n = gt_mask(up, bb)，nn = gt_mask(aa, up)
                t2 = bb ^ 0xFFFFFFFF
                g1 = ((up & t2) + (((up ^ t2) >> 1) & 0x7F7F7F7F)) & 0x80808080
                nb = ((g1 >> 7) + 0x7F7F7F7F) ^ 0x7F7F7F7F
                t2 = up ^ 0xFFFFFFFF
                g1 = ((aa & t2) + (((aa ^ t2) >> 1) & 0x7F7F7F7F)) & 0x80808080
                nc = ((g1 >> 7) + 0x7F7F7F7F) ^ 0x7F7F7F7F
                m = (nb | nc) ^ 0xFFFFFFFF
                pred = ((nb & aa) | (nc & bb)
                        | (((bb & m) - (up & m) + (aa & m)) & 0xFFFFFFFF))
                c2 = (((pred & v) << 1) + ((pred ^ v) & 0xFEFEFEFE)) & 0x01010100
                p = (pred + v - c2) & 0xFFFFFFFF
                cur[ci] = p
                pi += 1
                ci += 1
                ib += step
                up = u

        if step > 0:
            ib += skipblockbytes - npx
        else:
            ib += skipblockbytes + 1
        if i & 1:
            ib -= oddskip * npx


def _tlg6(d: bytes, pos: int, width: int, height: int, colors: int) -> bytes:
    n = len(d)
    if pos + 4 > n:
        raise ValueError("TLG6 头被截断")
    pos += 4                             # max_bit_length，只用来定缓冲区大小
    xbc = (width - 1) // _TLG6_W_BLOCK + 1
    ybc = (height - 1) // _TLG6_H_BLOCK + 1
    mcount = width // _TLG6_W_BLOCK
    frac = width - mcount * _TLG6_W_BLOCK

    if pos + 4 > n:
        raise ValueError("TLG6 filter 表被截断")
    fsize = _i32(d, pos)
    pos += 4
    if fsize < 0 or pos + fsize > n:
        raise ValueError("TLG6 filter 表越界")
    ftypes, _ = _lzss_decompress(d[pos:pos + fsize], xbc * ybc,
                                 bytearray(_LZSS_SEED), 0)
    pos += fsize

    stride = width * 4
    img = bytearray(height * stride)
    zero = 0xFF000000 if colors == 3 else 0
    prev = [zero] * width                # y = -1 的虚拟行
    cur = [0] * width
    pack = struct.Struct("<%dI" % width).pack

    for y in range(0, height, _TLG6_H_BLOCK):
        y_lim = y + _TLG6_H_BLOCK
        if y_lim > height:
            y_lim = height
        rows = y_lim - y
        count = rows * width
        pbuf = [0] * count               # 每个通道只写自己那个字节，其余保持 0
        for c in range(colors):
            if pos + 4 > n:
                raise ValueError("TLG6 通道头被截断")
            bitlen = _i32(d, pos)
            pos += 4
            method = (bitlen >> 30) & 3
            bitlen &= 0x3FFFFFFF
            if method:                   # 01 Gamma / 10 LZSS / 11 raw 都没实现
                raise ValueError("TLG6 熵编码方法 %d 不支持" % method)
            nbytes = (bitlen + 7) >> 3
            if pos + nbytes > n:
                raise ValueError("TLG6 位流越界")
            _golomb_decode(d[pos:pos + nbytes] + b"\x00" * 8, nbytes, count,
                           pbuf, c * 8)
            pos += nbytes

        ftbase = (y // _TLG6_H_BLOCK) * xbc
        _tlg6_transform(pbuf, rows, xbc, mcount, frac, ftypes, ftbase)

        for yy in range(y, y_lim):
            r = yy - y
            dirbit = (yy & 1) ^ 1
            oddskip = (y_lim - yy - 1) - r
            if mcount:                   # 8 宽的主块列
                _tlg6_line(prev, cur, width, _TLG6_W_BLOCK, 0, mcount,
                           ftypes, ftbase, rows * _TLG6_H_BLOCK, pbuf,
                           _TLG6_W_BLOCK * r, zero, oddskip, dirbit)
            if mcount != xbc:            # 右侧不满 8 的碎块列
                _tlg6_line(prev, cur, width, frac, mcount, xbc,
                           ftypes, ftbase, rows * _TLG6_H_BLOCK, pbuf,
                           frac * r, zero, oddskip, dirbit)
            img[yy * stride:(yy + 1) * stride] = pack(*cur)
            prev, cur = cur, prev
    return bytes(img)


# ---------------------------------------------------------------- 对外
def tlg_decode(d: bytes):
    """返回 (width, height, colors, BGRA bytes)，不是 TLG 返回 None"""
    meta = tlg_meta(d)
    if not meta:
        return None
    ver, colors, width, height, pos = meta
    if pos > len(d):
        return None
    if ver == 6:
        return width, height, colors, _tlg6(d, pos, width, height, colors)
    return width, height, colors, _tlg5(d, pos, width, height, colors)


@register_image("TLG (KiriKiri)")
def _tlg(data, ctx):
    try:
        res = tlg_decode(data)
        if not res:
            return None
        w, h, colors, buf = res
        im = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", w * 4, 1)
        # 24 位图 alpha 全是 0xFF；注意 Pillow 的 "RGB"+"BGR" 不认 4 字节跨距，
        # 所以先按 BGRA 读进来再转，别改成直接 frombuffer("RGB", ...)
        return im if colors == 4 else im.convert("RGB")
    except Exception:
        return None


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    # 说明：TLG 跟 zlib 没关系 —— TLG5 用的是 TVP 自己那套 LZSS，TLG6 是
    # Golomb 编码 + 用 LZSS 压的 filter type 表。所以这里按规范手工造流，
    # 不拿 zlib 硬套。

    class _BitWriter:
        """LSB 优先的位写入器，TLG6 的 Golomb 流就是这个位序"""

        def __init__(self):
            self.buf = bytearray()
            self.n = 0

        def put(self, value, bits):
            for k in range(bits):
                if not (self.n & 7):
                    self.buf.append(0)
                if (value >> k) & 1:
                    self.buf[-1] |= 1 << (self.n & 7)
                self.n += 1

    def _put_run(bw, count):
        """游程长度：p 个 0、一个 1、再 p 位余数"""
        p = count.bit_length() - 1
        bw.put(0, p)
        bw.put(1, 1)
        bw.put(count - (1 << p), p)

    def _golomb_encode(vals):
        """把一串 0..255 的增量按 TLG6 的 Golomb 编码打成位流"""
        bw = _BitWriter()
        zero = not vals or vals[0] == 0
        bw.put(0 if zero else 1, 1)
        i = 0
        n = 3
        a = 0
        while i < len(vals):
            j = i
            while j < len(vals) and (vals[j] == 0) == zero:
                j += 1
            _put_run(bw, j - i)
            if not zero:
                for v in vals[i:j]:
                    k = _GOLOMB_K[(a << 2) | n] if a < 1024 else 255
                    e = (v << 1) - 1
                    bw.put(0, e >> k)
                    bw.put(1, 1)
                    bw.put(e & ((1 << k) - 1), k)
                    a += e >> 1
                    n -= 1
                    if n < 0:
                        a >>= 1
                        n = 3
            i = j
            zero = not zero
        return bytes(bw.buf), bw.n

    def _lzss_literals(data):
        """只用字面量的 LZSS 压缩（每 8 个字节一个 0x00 标志字节）"""
        comp = bytearray()
        for i in range(0, len(data), 8):
            comp.append(0)
            comp += data[i:i + 8]
        return bytes(comp)

    def _tlg5_build(w, h, colors, chans):
        """造 TLG5：块高 = 整图高度，第一个通道走 LZSS，其余走 raw"""
        out = bytearray(b"TLG5.0\x00raw\x1a")
        out.append(colors)
        out += struct.pack("<II", w, h)
        out += struct.pack("<i", h)              # blockheight
        out += b"\x00\x00\x00\x00"               # 块大小表，解码器会跳过
        for c in range(colors):
            data = bytes(chans[c])
            if c == 0:
                comp = _lzss_literals(data)
                out += b"\x00" + struct.pack("<i", len(comp)) + comp
            else:
                out += b"\x01" + struct.pack("<i", len(data)) + data
        return bytes(out)

    def _tlg6_build(w, h, colors, ftype, deltas):
        """造 TLG6：filter type 全用 ftype，deltas[y][x] = [b, g, r, a]"""
        out = bytearray(b"TLG6.0\x00raw\x1a")
        out.append(colors)
        out += b"\x00\x00\x00"
        out += struct.pack("<II", w, h)
        xbc = (w - 1) // 8 + 1
        ybc = (h - 1) // 8 + 1
        mcount = w // 8
        frac = w - mcount * 8
        out += struct.pack("<i", 0)              # max_bit_length
        out += struct.pack("<i", len(_lzss_literals(bytes([ftype]) * (xbc * ybc))))
        out += _lzss_literals(bytes([ftype]) * (xbc * ybc))
        for y in range(0, h, 8):
            rows = min(8, h - y)
            count = rows * w
            for c in range(colors):
                vals = [0] * count
                for r in range(rows):
                    yy = y + r
                    step = 1 if ((yy & 1) ^ 1) else -1
                    oddskip = (rows - 1 - r) - r
                    for i in range(xbc):
                        if i < mcount:
                            bw, sx = 8, i * 8
                            pos = 8 * r + rows * 8 * i
                        else:
                            bw, sx = frac, mcount * 8
                            pos = frac * r + rows * 8 * mcount
                        if i & 1:
                            pos += oddskip * bw
                        if step < 0:
                            pos += bw - 1
                        for k in range(bw):
                            vals[pos + k * step] = deltas[yy][sx + k][c]
                data, nbits = _golomb_encode(vals)
                out += struct.pack("<i", nbits)
                out += data
        return bytes(out)

    # ---- 1) TLG5 4x4，4 通道，LZSS + raw 混着来
    ch = [bytearray() for _ in range(4)]
    for _ in range(4):
        ch[0] += bytes([200, 0, 0, 0])       # 蓝：只有第一个像素有增量
        ch[1] += bytes([0, 0, 0, 0])         # 绿
        ch[2] += bytes([100, 0, 0, 0])       # 红
        ch[3] += bytes([255, 0, 0, 0])       # alpha
    t5 = _tlg5_build(4, 4, 4, ch)
    im5 = _tlg(t5, None)
    assert im5 is not None, "TLG5 没解出来"
    assert im5.size == (4, 4), im5.size
    assert im5.mode == "RGBA", im5.mode
    assert len(im5.tobytes()) == 4 * 4 * 4, "TLG5 像素数不对"
    # Pillow 的 getpixel 给的是 (R, G, B, A)，注意和 BGRA 字节序反过来
    rows5 = [(100, 0, 200, 255), (200, 0, 144, 254),
             (44, 0, 88, 253), (144, 0, 32, 252)]
    want5 = [c for c in rows5 for _ in range(4)]
    got5 = [im5.getpixel((x, y)) for y in range(4) for x in range(4)]
    assert got5 == want5, got5
    print("TLG5 4x4 RGBA  OK  size=%s mode=%s px=%d" %
          (im5.size, im5.mode, im5.size[0] * im5.size[1]))

    # 首字节被取反（0xAB）的变体：应该解出同一张图
    im5x = _tlg(bytes([0xAB]) + t5[1:], None)
    assert im5x is not None and im5x.size == (4, 4), "0xAB 变体的 TLG5 没解出来"
    assert im5x.tobytes() == im5.tobytes(), "0xAB 变体结果不一致"
    print("TLG5 4x4 obfuscated header OK  size=%s" % (im5x.size,))

    # ---- 1b) TLG5 3 通道（没有 alpha），走另一条合成分支
    ch3 = [bytearray() for _ in range(3)]
    for _ in range(4):
        ch3[0] += bytes([200, 0, 0, 0])
        ch3[1] += bytes([0, 0, 0, 0])
        ch3[2] += bytes([100, 0, 0, 0])
    t5b = _tlg5_build(4, 4, 3, ch3)
    im5b = _tlg(t5b, None)
    assert im5b is not None, "3 通道的 TLG5 没解出来"
    assert im5b.size == (4, 4) and im5b.mode == "RGB", (im5b.size, im5b.mode)
    assert len(im5b.tobytes()) == 4 * 4 * 3, "3 通道 TLG5 像素数不对"
    want5b = [(100, 0, 200), (200, 0, 144), (44, 0, 88), (144, 0, 32)]
    got5b = [im5b.getpixel((0, y)) for y in range(4)]
    assert got5b == want5b, got5b
    print("TLG5 4x4 RGB   OK  size=%s mode=%s px=%d" %
          (im5b.size, im5b.mode, im5b.size[0] * im5b.size[1]))

    # ---- 2) TLG6 4x4，3 通道，filter type 0（MED 预测）
    # 增量全取 <= 10：Golomb 的 k 一开始是 0，值太大编码会切到转义路径，自测不折腾那个
    d6 = [[[0, 0, 0] for _ in range(4)] for _ in range(4)]
    d6[0][0] = [7, 5, 8]
    d6[0][1] = [3, 0, 2]
    d6[0][2] = [0, 1, 0]
    d6[0][3] = [1, 1, 1]
    for y in range(1, 4):
        for x in range(4):
            d6[y][x] = [1, 1, 1]
    t6 = _tlg6_build(4, 4, 3, 0, [[[v[0], v[1], v[2], 0] for v in row] for row in d6])
    im6 = _tlg(t6, None)
    assert im6 is not None, "TLG6 没解出来"
    assert im6.size == (4, 4), im6.size
    assert im6.mode == "RGB", im6.mode
    assert len(im6.tobytes()) == 4 * 4 * 3, "TLG6 像素数不对"
    # 第一行没有上一行，等价于纯前缀和：像素 = 上一个像素 + 增量
    want6 = [(8, 5, 7), (10, 5, 10), (10, 6, 10), (11, 7, 11)]
    got6 = [im6.getpixel((x, 0)) for x in range(4)]
    assert got6 == want6, got6
    print("TLG6 4x4 RGB   OK  size=%s mode=%s px=%d" %
          (im6.size, im6.mode, im6.size[0] * im6.size[1]))

    # ---- 3) TLG6 12x3，4 通道，filter type 2（色度反相关 + MED），跨主块/碎块
    d12 = [[[0, 0, 0, 0] for _ in range(12)] for _ in range(3)]
    d12[0][0] = [7, 5, 8, 0]
    d12[0][1] = [3, 0, 2, 0]
    d12[0][2] = [0, 1, 0, 0]
    d12[0][3] = [1, 1, 1, 0]
    for y in range(1, 3):
        for x in range(12):
            d12[y][x] = [1, 2, 3, 4]
    t12 = _tlg6_build(12, 3, 4, 2, d12)
    im12 = _tlg(t12, None)
    assert im12 is not None, "12x3 的 TLG6 没解出来"
    assert im12.size == (12, 3), im12.size
    assert im12.mode == "RGBA", im12.mode
    assert len(im12.tobytes()) == 12 * 3 * 4, "12x3 像素数不对"
    # 4 通道时初始像素是 0，第一行 = 变换后的增量前缀和
    want12 = [(13, 5, 12, 0), (15, 5, 15, 0), (16, 6, 16, 0), (18, 7, 18, 0)]
    got12 = [im12.getpixel((x, 0)) for x in range(4)]
    assert got12 == want12, got12
    assert im12.getpixel((11, 0)) == (18, 7, 18, 0), im12.getpixel((11, 0))
    print("TLG6 12x3 RGBA OK  size=%s mode=%s px=%d" %
          (im12.size, im12.mode, im12.size[0] * im12.size[1]))

    # ---- 4) TLG6 4x2，4 通道，filter type 1（AVG 预测）走另一条分支
    d4 = [[[7, 8, 9, 10] for _ in range(4)] for _ in range(2)]
    t4 = _tlg6_build(4, 2, 4, 1, d4)
    im4 = _tlg(t4, None)
    assert im4 is not None, "AVG 分支没解出来"
    assert im4.size == (4, 2), im4.size
    assert len(im4.tobytes()) == 4 * 2 * 4, "4x2 像素数不对"
    assert im4.getpixel((0, 0)) == (9, 8, 7, 10), im4.getpixel((0, 0))
    print("TLG6 4x2 RGBA  OK  size=%s mode=%s px=%d" %
          (im4.size, im4.mode, im4.size[0] * im4.size[1]))

    # ---- 5) 不是 TLG 的东西必须老实返回 None
    assert _tlg(b"", None) is None
    assert _tlg(b"not a tlg image at all, just some random bytes" * 4, None) is None
    assert _tlg(b"TLG5.0\x00raw\x1a" + b"\x00" * 40, None) is None
    print("non-TLG data   OK  returns None")

    print("ALL SELF-TESTS PASSED")
