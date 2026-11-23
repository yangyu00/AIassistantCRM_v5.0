"""注册 xlsx 的源文件侧：内容指纹 + 按 registry 抽列去重。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .models import TableSpec
from .protocols import FileFingerprint, MissingColumn, ParsedRows


def _cast(value, typ):
    """TEXT 原样。"""
    if typ == "TEXT":
        return value
    try:
        return int(value) if typ == "INTEGER" else float(value)
    except (ValueError, TypeError):
        return 0 if typ == "INTEGER" else 0.0


class OpenpyxlExcelSource:
    """用 openpyxl 只读打开 xlsx。"""

    def fingerprint(self, path: Path) -> FileFingerprint:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        st = path.stat()
        return FileFingerprint(sha256=h.hexdigest(), size=st.st_size, mtime=st.st_mtime)

    def read_rows(self, spec: TableSpec, path: Path) -> ParsedRows:
        from openpyxl import load_workbook  # 延迟导入

        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[spec.sheet] if isinstance(spec.sheet, str) else wb.worksheets[spec.sheet]
            rows = ws.iter_rows(values_only=True)

            header = None
            for i, row in enumerate(rows, 1):
                if i == spec.header_row:
                    header = ["" if v is None else str(v).strip() for v in row]
                    break
            if header is None:
                raise RuntimeError(f"表头行 {spec.header_row} 不存在")

            positions = []
            for c in spec.columns:
                if c.column_index is not None:
                    pos = c.column_index - 1
                    if pos >= len(header):
                        raise RuntimeError(
                            f"column_index {c.column_index} 超出表头列数")
                elif c.source in header:
                    pos = header.index(c.source)
                elif c.optional:
                    pos = None
                else:
                    raise MissingColumn(f"缺少必需表头列 [{c.source}]")
                positions.append(pos)

            by_name = {c.column: i for i, c in enumerate(spec.columns)}
            pk_idx = [by_name[k] for k in spec.primary_key]

            out, skipped_empty, duplicated = {}, 0, 0
            for row in rows:
                values = []
                for c, pos in zip(spec.columns, positions):
                    raw = "" if pos is None or pos >= len(row) or row[pos] is None else row[pos]
                    val = str(raw)
                    if c.strip:
                        val = val.strip()
                    if not val and c.default:
                        val = c.default
                    values.append(_cast(val, c.type))
                key = tuple(values[i] for i in pk_idx)
                if any(v is None or str(v).strip() == "" for v in key):
                    skipped_empty += 1
                    continue
                if key in out:
                    duplicated += 1
                out[key] = tuple(values)

            return ParsedRows(
                rows=tuple(out.values()),
                skipped_empty=skipped_empty,
                duplicated=duplicated,
                header=tuple(header),
            )
        finally:
            wb.close()


__all__ = ["OpenpyxlExcelSource"]
