# -*- coding: utf-8 -*-
"""诊断新线：取数（query）、纯逻辑（logic）、业务对象（rewrite）、口径（constants）。

包根用延迟导出：``query`` 会加载 ``sftp_avl2db.data_query``（读包内 .env），
不能让 ``from services.diagnosis.logic import ...`` 这种纯逻辑单测也跟着读配置。
在线侧 ``from services.diagnosis import build_diagnosis_record`` 才触发加载。
"""
_LAZY_EXPORTS = {
    "build_diagnosis_record": ("query", "build_diagnosis_record"),
    "ProductInfoByDiagnosis": ("rewrite", "ProductInfoByDiagnosis"),
}


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        import importlib
        mod, attr = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module("." + mod, __name__), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = ["build_diagnosis_record", "ProductInfoByDiagnosis"]
