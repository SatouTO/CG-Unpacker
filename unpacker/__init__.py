"""游戏图片解包器 —— 插件式归档/图片解码框架"""
__version__ = "1.0.0"

from .registry import (  # noqa: F401
    archive_formats,
    image_decoders,
    open_archive,
    open_archive_ex,
    register_archive,
    register_image,
)
from . import images  # noqa: F401  注册内置图片解码器
from .formats import afa, ald, xp3, zipfmt  # noqa: F401  注册内置归档格式
from .extract import (  # noqa: F401
    ExtractContext,
    ExtractOptions,
    Stats,
    decode_image,
    extract,
    extract_file,
)
