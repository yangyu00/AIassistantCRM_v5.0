"""``call_stat``：调用量统计独立模块（包内配置 + 包内产物 + 可独立调用）。"""
from .config import CallStatConfig, ENV_PATH, RUNTIME_DIR, load_config, warn

# 延迟导出：(模块名, 属性名)。import 本包不加载任何含 I/O 或第三方依赖的模块。
_LAZY_EXPORTS = {
    # 配置与计数门面
    "get_config": ("counter", "get_config"),
    "configure": ("counter", "configure"),
    "startup": ("counter", "startup"),
    "record_call": ("counter", "record_call"),
    "flush": ("counter", "flush"),
    "get_call_count": ("counter", "get_call_count"),
    "get_month_counts": ("counter", "get_month_counts"),
    # 查询纯函数 / 响应组装
    "is_valid_date": ("query", "is_valid_date"),
    "is_valid_month": ("query", "is_valid_month"),
    "validate_params": ("query", "validate_params"),
    "error_response": ("query", "error_response"),
    "date_result": ("query", "date_result"),
    "month_result": ("query", "month_result"),
    # FastAPI 集成（fastapi 在 build_router 内延迟导入）
    "count_call_middleware": ("web", "count_call_middleware"),
    "build_router": ("web", "build_router"),
    # CLI
    "cli_main": ("cli", "main"),
    "build_arg_parser": ("cli", "build_arg_parser"),
}


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        import importlib
        mod, attr = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module("." + mod, __name__), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    # 配置（顶层，零 I/O）
    "CallStatConfig", "load_config", "ENV_PATH", "RUNTIME_DIR", "warn",
    # 门面（延迟）
    "get_config", "configure", "startup", "record_call", "flush",
    "get_call_count", "get_month_counts",
    # 查询（延迟）
    "is_valid_date", "is_valid_month", "validate_params", "error_response",
    "date_result", "month_result",
    # 集成（延迟）
    "count_call_middleware", "build_router",
    # CLI（延迟）
    "cli_main", "build_arg_parser",
]
