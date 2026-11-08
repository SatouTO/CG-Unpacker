"""归档格式注册表 + 图片解码器注册表"""
from typing import Callable, Optional

from .core import Archive, Entry, FileView, NotThisFormat

Detector = Callable[[FileView], Optional[list[Entry]]]

_ARCHIVES: list[tuple[str, str, Detector]] = []
_IMAGE_DECODERS: list[tuple[str, Callable]] = []


def register_archive(name: str, desc: str):
    """注册一个归档格式探测器

    探测器签名: fn(view) -> list[Entry] | None
    不是自己的格式就 return None（或抛 NotThisFormat）
    """
    def deco(fn: Detector):
        _ARCHIVES.append((name, desc, fn))
        return fn
    return deco


def register_image(desc: str):
    """注册一个图片解码器

    解码器签名: fn(data, ctx) -> PIL.Image.Image | None
    ctx 是 ExtractContext，可以拿到同归档里的其他文件（DCF 差分要用）
    """
    def deco(fn):
        _IMAGE_DECODERS.append((desc, fn))
        return fn
    return deco


def archive_formats() -> list[tuple[str, str]]:
    return [(n, d) for n, d, _ in _ARCHIVES]


def image_decoders() -> list[tuple[str, Callable]]:
    return list(_IMAGE_DECODERS)


def open_archive(path: str) -> Optional[Archive]:
    """依次问每个探测器的意见，第一个认领的就用它"""
    view = FileView(path)
    for name, _desc, detector in _ARCHIVES:
        try:
            result = detector(view)
        except NotThisFormat:
            continue
        except Exception:
            continue
        if isinstance(result, Archive):
            result.format = result.format or name
            return result
        if result:
            return Archive(view, name, result)
    view.close()
    return None


def open_archive_ex(path: str) -> tuple[Optional[Archive], str]:
    """跟 open_archive 一样，但会返回失败原因，方便 GUI 提示"""
    view = FileView(path)
    errors = []
    for name, _desc, detector in _ARCHIVES:
        try:
            result = detector(view)
        except NotThisFormat as e:
            errors.append(f"{name}: {e}")
            continue
        except Exception as e:  # 探测过程崩了不算致命
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if isinstance(result, Archive):
            result.format = result.format or name
            return result, ""
        if result:
            return Archive(view, name, result), ""
    view.close()
    return None, "；".join(errors) if errors else "没有可识别的格式"
