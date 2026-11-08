"""解包引擎：负责落盘、命名、图片转换"""
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .core import Archive, Entry
from .registry import image_decoders

IMAGE_EXTS = {".qnt", ".dcf", ".ajp", ".pcf", ".tlg", ".g00",
              ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif",
              ".tga", ".tif", ".tiff", ".ico", ".psd"}

_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')
_DEVICE = re.compile(r"(?:CON|AUX|PRN|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])", re.I)


@dataclass
class ExtractOptions:
    out_dir: str
    only_images: bool = True
    convert_png: bool = True
    overwrite: bool = False
    flatten: bool = False
    should_stop: Optional[Callable[[], bool]] = None
    base_dir: Optional[str] = None  # 用户选定的位置，out_dir 可能还包含自动添加的包名。


@dataclass
class Stats:
    converted: int = 0
    raw: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def summary(self) -> str:
        bits = [f"转换 {self.converted} 张", f"原样导出 {self.raw} 个",
                f"跳过 {self.skipped} 个"]
        if self.failed:
            bits.append(f"失败 {self.failed} 个")
        bits.append(f"耗时 {self.elapsed:.1f}s")
        return "，".join(bits)


class ExtractContext:
    """给解码器用的上下文：能按名字拿到同归档里的别的文件（DCF 找底图要用）"""

    def __init__(self, archive: Archive):
        self.archive = archive
        self.memo: dict[str, object] = {}
        self._map: dict[str, Entry] = {}
        for e in archive.entries:
            self._map.setdefault(_norm(e.name).lower(), e)
        self._cache: dict[str, bytes] = {}

    def get(self, name: str) -> Optional[bytes]:
        key = _norm(name).lower()
        entry = self._map.get(key)
        if entry is None:
            return None
        if key in self._cache:
            return self._cache[key]
        try:
            data = self.archive.read(entry)
        except Exception:
            return None
        if len(self._cache) < 8:
            self._cache[key] = data
        return data


def _norm(name: str) -> str:
    return name.replace("\uff0f", "/").replace("\\", "/")


def safe_parts(name: str) -> list[str]:
    parts = []
    for piece in _norm(name).split("/"):
        piece = piece.strip().strip(". ")
        piece = _BAD.sub("_", piece)
        if piece:
            if _DEVICE.fullmatch(piece.partition(".")[0].rstrip()):
                piece = "_" + piece
            parts.append(piece)
    return parts or ["unnamed"]


def is_image_entry(entry: Entry) -> bool:
    return entry.ext in IMAGE_EXTS or entry.meta.get("image", False)


def decode_image(data: bytes, ctx: Optional[ExtractContext]):
    """挨个问图片解码器，返回 (PIL 图, 解码器说明)"""
    for desc, fn in image_decoders():
        try:
            img = fn(data, ctx)
        except Exception:
            continue
        if img is not None:
            return img, desc
    return None, ""


def read_entry(archive: Archive, entry: Entry, ctx: ExtractContext, *, convert=True):
    """预览和导出共用读取路径；容器帧可直接绘制，其他条目保留原字节供导出。"""
    if convert:
        decode_frame = getattr(archive, "decode_frame", None)
        if decode_frame is not None:
            return None, decode_frame(entry), "G00 (RealLive)"
    data = archive.read(entry)
    if not convert:
        return data, None, ""
    image, desc = decode_image(data, ctx)
    return data, image, desc


def _inside(root: str, path: str) -> str:
    resolved = os.path.realpath(path)
    try:
        common = os.path.commonpath([root, resolved])
    except ValueError:  # Windows 上目标在另一驱动器
        common = ""
    if os.path.normcase(common) != os.path.normcase(root):
        raise ValueError(f"路径超出保存目录：{path}")
    return resolved


def _publish(temp: str, path: str, root: str, next_index: dict[str, int], overwrite: bool):
    """同目录发布完整文件。跨进程争用同名时，重试未占用的下一个编号。"""
    _inside(root, os.path.dirname(path))
    stem, ext = os.path.splitext(path)
    key = os.path.normcase(path)
    for index in range(next_index.get(key, 0), 10000):
        candidate = f"{stem} ({index}){ext}" if index else path
        candidate_key = os.path.normcase(candidate)
        if candidate_key in next_index:
            continue
        if overwrite and index == 0:
            _inside(root, candidate)
            os.replace(temp, candidate)
        else:
            if os.path.lexists(candidate):
                continue
            try:
                if os.name == "nt":
                    os.rename(temp, candidate)  # Windows 的 rename 拒绝覆盖已有文件。
                else:
                    os.link(temp, candidate)
            except FileExistsError:
                continue
        next_index[key] = index + 1
        next_index.setdefault(candidate_key, 1)
        return
    raise RuntimeError("同名文件太多")


def _write_entry(directory, name, data, img, root, next_index, overwrite):
    directory = _inside(root, directory)
    temp = tempfile.NamedTemporaryFile(dir=directory, prefix=".cg-", suffix=".tmp", delete=False)
    try:
        with temp:
            if img is None:
                temp.write(data)
            else:
                if img.mode not in ("RGB", "RGBA", "L", "LA"):
                    img = img.convert("RGBA")
                img.save(temp, "PNG")
        _publish(temp.name, os.path.join(directory, name), root, next_index, overwrite)
    finally:
        try:
            os.unlink(temp.name)
        except FileNotFoundError:
            pass  # Windows 通过重命名发布，临时文件名已经消失。


def extract(archive: Archive, entries: list[Entry], opts: ExtractOptions,
            progress: Optional[Callable[[int, int, str], None]] = None) -> Stats:
    root = os.path.realpath(opts.base_dir if opts.base_dir is not None else opts.out_dir)
    out_dir = _inside(root, opts.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    ctx = ExtractContext(archive)
    next_index: dict[str, int] = {}
    stats = Stats()
    total = len(entries)

    for i, entry in enumerate(entries, 1):
        if opts.should_stop and opts.should_stop():
            stats.errors.append("用户取消")
            break
        if progress:
            progress(i - 1, total, entry.name)
        if opts.only_images and not is_image_entry(entry):
            stats.skipped += 1
            continue
        try:
            parts = safe_parts(entry.name)
            if opts.flatten:
                parts = parts[-1:]
            rel_dir = _inside(out_dir, os.path.join(out_dir, *parts[:-1]))
            os.makedirs(rel_dir, exist_ok=True)
            data, img, _ = read_entry(archive, entry, ctx, convert=opts.convert_png)
            name = parts[-1] if img is None else os.path.splitext(parts[-1])[0] + ".png"
            _write_entry(rel_dir, name, data, img, out_dir, next_index, opts.overwrite)
            if img is not None:
                stats.converted += 1
            else:
                stats.raw += 1
        except Exception as e:
            stats.failed += 1
            stats.errors.append(f"{entry.name}: {type(e).__name__}: {e}")

    if progress:
        progress(total, total, "")
    return stats


def extract_file(path: str, opts: ExtractOptions,
                 progress: Optional[Callable[[int, int, str], None]] = None):
    from .registry import open_archive_ex

    archive, err = open_archive_ex(path)
    if archive is None:
        raise ValueError(err or "无法识别的归档格式")
    try:
        return extract(archive, archive.entries, opts, progress)
    finally:
        archive.close()
