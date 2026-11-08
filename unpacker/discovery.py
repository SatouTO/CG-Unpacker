"""递归识别可打开的归档，只保留列表信息。"""
import os
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .registry import open_archive


@dataclass(frozen=True)
class ArchiveInfo:
    path: str
    format: str
    entry_count: int


@dataclass
class ScanResult:
    archives: list[ArchiveInfo] = field(default_factory=list)
    scanned: int = 0
    unreadable: int = 0
    cancelled: bool = False


def scan_archives(paths: Iterable[str | os.PathLike], *,
                  should_stop: Callable[[], bool] | None = None,
                  progress: Callable[[int, int, str], None] | None = None) -> ScanResult:
    result = ScanResult()
    pending = [os.path.abspath(path) for path in paths]
    seen = set()
    next_report = 0.0
    while pending:
        if should_stop and should_stop():
            break
        path = pending.pop()
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            info = os.lstat(path)
            # Windows 的目录联接不一定带符号链接模式位。
            if stat.S_ISLNK(info.st_mode) or (
                os.name == "nt" and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
            ):
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.extend(os.path.join(path, name) for name in os.listdir(path))
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if progress and time.monotonic() >= next_report:
                progress(result.scanned, len(result.archives), path)
                next_report = time.monotonic() + 0.12
            result.scanned += 1
            archive = open_archive(path, should_stop=should_stop)
            if archive is not None:
                with archive:
                    result.archives.append(ArchiveInfo(path, archive.format, len(archive.entries)))
        except OSError:
            result.unreadable += 1
    result.cancelled = bool(should_stop and should_stop())
    result.archives.sort(key=lambda item: (item.path.casefold(), item.path))
    return result
