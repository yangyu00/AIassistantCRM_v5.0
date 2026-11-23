"""单表导入编排：文件存在性 → 指纹比对 → 抽列 → 单事务整表覆盖 + version 递增。"""
from __future__ import annotations

from .models import Registry, TableSpec
from .protocols import ExcelSource, ImportResult, WritableRepository


class TableImporter:
    """一次 ``import_table`` 处理一张注册维表，结果以 ``ImportResult`` 返回。"""

    def __init__(self, registry: Registry, source: ExcelSource,
                 repository: WritableRepository) -> None:
        self._registry = registry
        self._source = source
        self._repo = repository

    def import_table(self, spec: TableSpec, *, force: bool = False) -> ImportResult:
        path = spec.source
        if not path.is_file():
            return ImportResult(table=spec.table, status="missing", path=str(path))

        fp = self._source.fingerprint(path)
        self._repo.ensure_meta()
        self._repo.ensure_table(spec)

        prev = self._repo.previous_version(spec)
        if prev is not None and not force and prev[1] == fp.sha256:
            return ImportResult(table=spec.table, status="unchanged", version=prev[0])

        parsed = self._source.read_rows(spec, path)
        version = (prev[0] if prev is not None else 0) + 1
        self._repo.replace_all(spec, parsed, version, fp, path)
        return ImportResult(
            table=spec.table,
            status="updated",
            version=version,
            rows=len(parsed.rows),
            skipped_empty=parsed.skipped_empty,
            duplicated=parsed.duplicated,
            sha256=fp.sha256,
            path=str(path),
        )


__all__ = ["TableImporter"]
