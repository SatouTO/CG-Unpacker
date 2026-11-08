"""解包引擎：负责落盘、命名、图片转换"""
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .core import Archive, Entry
from .registry import image_decoders

IMAGE_EXTS = {".qnt", ".dcf", ".ajp", ".pcf", ".png", ".jpg", ".jpeg", ".bmp",
              ".webp", ".gif", ".tga", ".tif", ".tiff", ".ico", ".psd"}

_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')


@dataclass
class ExtractOptions:
    out_dir: str
    only_images: bool = True
    convert_png: bool = True
    overwrite: bool = False
    flatten: bool = False
    should_stop: Optional[Callable[[], bool]] = None


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
        piece = piece.strip().strip(".")
        piece = _BAD.sub("_", piece)
        if piece:
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


def _unique_path(path: str, used: set[str], overwrite: bool) -> str:
    if overwrite and path not in used:
        used.add(path)
        return path
    if path not in used and not os.path.exists(path):
        used.add(path)
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{stem} ({i}){ext}"
        if cand not in used and not os.path.exists(cand):
            used.add(cand)
            return cand
        i += 1
        if i > 9999:
            raise RuntimeError("同名文件太多")


def extract(archive: Archive, entries: list[Entry], opts: ExtractOptions,
            progress: Optional[Callable[[int, int, str], None]] = None) -> Stats:
    os.makedirs(opts.out_dir, exist_ok=True)
    ctx = ExtractContext(archive)
    used: set[str] = set()
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
            data = archive.read(entry)
        except Exception as e:
            stats.failed += 1
            stats.errors.append(f"{entry.name}: {type(e).__name__}: {e}")
            continue

        parts = safe_parts(entry.name)
        if opts.flatten:
            parts = [parts[-1]]
        rel_dir = os.path.join(opts.out_dir, *parts[:-1]) if len(parts) > 1 else opts.out_dir
        os.makedirs(rel_dir, exist_ok=True)
        stem, ext = os.path.splitext(parts[-1])

        img = None
        desc = ""
        if opts.convert_png:
            img, desc = decode_image(data, ctx)
        if img is not None:
            try:
                path = _unique_path(os.path.join(rel_dir, stem + ".png"), used, opts.overwrite)
                if img.mode not in ("RGB", "RGBA", "L", "LA"):
                    img = img.convert("RGBA")
                img.save(path, "PNG")
                stats.converted += 1
            except Exception as e:
                stats.failed += 1
                stats.errors.append(f"{entry.name}: 保存失败 {e}")
        else:
            try:
                path = _unique_path(os.path.join(rel_dir, parts[-1]), used, opts.overwrite)
                with open(path, "wb") as f:
                    f.write(data)
                stats.raw += 1
            except Exception as e:
                stats.failed += 1
                stats.errors.append(f"{entry.name}: 保存失败 {e}")

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
