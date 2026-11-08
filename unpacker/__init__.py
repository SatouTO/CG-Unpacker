"""游戏图片解包器 —— 插件式归档/图片解码框架"""
__version__ = "1.4.0"

import importlib
import pkgutil

from .registry import (  # noqa: F401
    archive_formats,
    image_decoders,
    open_archive,
    open_archive_ex,
    register_archive,
    register_image,
)

from . import images        # noqa: F401  内置 QNT / DCF / AJP / 通用图片
from . import formats       # noqa: F401  formats 包内部会自动 import 所有归档模块
from .formats import reallive  # noqa: F401  RealLive G00/.dat 归档
from . import images_g00       # noqa: F401  RealLive G00 图片解码器
from . import images_tlg       # noqa: F401  KiriKiri TLG5 / TLG6 图片解码器
from . import extract       # noqa: F401

# 显式名单：PyInstaller 打包时它静态分析不到 pkgutil 动态导入，必须点名
_BUILTIN_FORMATS = ("afa", "ald", "npa", "pfs", "reallive", "xp3", "ypf", "zipfmt")
_BUILTIN_IMAGES = ("images_tlg", "images_g00")

for _name in _BUILTIN_FORMATS:
    try:
        importlib.import_module(f"{__name__}.formats.{_name}")
    except Exception as _e:
        print(f"[unpacker] 归档模块 {_name} 加载失败: {_e}")

for _name in _BUILTIN_IMAGES:
    try:
        importlib.import_module(f"{__name__}.{_name}")
    except Exception as _e:
        print(f"[unpacker] 图片模块 {_name} 加载失败: {_e}")

# 额外的图片解码器（images_xxx.py）自动加载，新增格式不用改这里
for _m in pkgutil.iter_modules(__path__):
    if _m.name.startswith("images_"):
        try:
            importlib.import_module(f"{__name__}.{_m.name}")
        except Exception as _e:  # 单个解码器坏了不影响其他
            print(f"[unpacker] 跳过图片解码器 {_m.name}: {_e}")

from .extract import (  # noqa: F401,E402
    ExtractContext,
    ExtractOptions,
    Stats,
    decode_image,
    extract,
    extract_file,
)
