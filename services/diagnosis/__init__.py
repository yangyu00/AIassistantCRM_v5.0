# -*- coding: utf-8 -*-
"""诊断新线：取数（query）、纯逻辑（logic）、业务对象（rewrite）、口径（constants）、
卖点维表兜底（fallback）。

包根用延迟导出：``query`` 会加载 ``services.reader``（装配读取侧 profiles，
读包内 .env），不能让 ``from services.diagnosis.logic import ...`` 这种纯逻辑
单测也跟着读配置。在线侧
``from services.diagnosis import build_diagnosis_record`` 才触发加载。
``fallback`` / ``logic`` 只依赖标准库，可单独导入做离线单测。
"""
_LAZY_EXPORTS = {
    "build_diagnosis_record": ("query", "build_diagnosis_record"),
    "ProductInfoByDiagnosis": ("rewrite", "ProductInfoByDiagnosis"),
    "build_fallback_record": ("fallback", "build_fallback_record"),
    "pick_script_text": ("fallback", "pick_script_text"),
    "parse_tag_script": ("fallback", "parse_tag_script"),
    "FALLBACK_OMIT_SECTIONS": ("fallback", "FALLBACK_OMIT_SECTIONS"),
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
    "build_diagnosis_record", "ProductInfoByDiagnosis",
    "build_fallback_record", "pick_script_text", "parse_tag_script",
    "FALLBACK_OMIT_SECTIONS",
]
