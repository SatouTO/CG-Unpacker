"""内置归档格式：丢一个模块进这个目录就会自动注册，不用改这里"""
import importlib
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    if _m.name.startswith("_"):
        continue
    try:
        importlib.import_module(f"{__name__}.{_m.name}")
    except Exception as _e:  # 单个格式写坏了不影响其他格式
        print(f"[unpacker] 跳过格式模块 {_m.name}: {_e}")
