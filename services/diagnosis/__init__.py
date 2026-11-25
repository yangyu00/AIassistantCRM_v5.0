"""诊断：8 表取数、纯逻辑、六段话术对象、口径常量。"""
_LAZY_EXPORTS = {
    "build_diagnosis_record": ("query", "build_diagnosis_record"),
    "ProductInfoByDiagnosis": ("rewrite", "ProductInfoByDiagnosis"),
    "collect_tag_names": ("logic", "collect_tag_names"),
    "replace_tag_placeholders": ("logic", "replace_tag_placeholders"),
    "apply_tag_values": ("logic", "apply_tag_values"),
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
    "collect_tag_names", "replace_tag_placeholders", "apply_tag_values",
]
