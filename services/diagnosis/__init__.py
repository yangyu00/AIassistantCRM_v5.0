"""诊断：取数、纯逻辑、业务对象、口径常量与卖点维表兜底。"""
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
