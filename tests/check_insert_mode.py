"""写入模式契约自检：INSERT SQL 只在 ``TableSchema.insert_sql()`` 生成。

不打任何服务、不连 SFTP。覆盖两件事：

1. ``SqliteSink`` 按「表有没有物理主键 + 是否全量替换」单点推导出本次写入
   的 ``insert_mode``，三种组合的 SQL 文本与重构前逐字符一致；
2. ``TableSchema.insert_sql()`` 是唯一 SQL 来源：默认按表能力推导，无物理
   主键的表显式指定 replace / upsert 会报错而不是生成跑不起来的 SQL；
   ``build_sink(insert_mode=...)`` 注入点仍可覆盖有物理主键的表。

用法：``python3 tests/check_insert_mode.py``
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sftp_avl2db.config import load_profiles                # noqa: E402
from sftp_avl2db.sink import build_sink                     # noqa: E402
from sftp_avl2db.sink.schema import (                       # noqa: E402
    INSERT_MODES, TableSchema)

_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' +
          (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


def main():
    profiles = load_profiles(runtime_dir=tempfile.mkdtemp(prefix="insert_mode_"))

    def with_drop(name, value):
        p = profiles[name]
        return replace(p, retention=replace(
            p.retention, drop_before_insert=value))

    print("=== 1. 单一决策点：三种写入模式 ===")
    s_append = build_sink(with_drop("F09627", 0))
    s_replace = build_sink(with_drop("F09627", 1))
    s_plain = build_sink(profiles["F09633"])
    check("有物理主键 + 追加 → upsert", s_append.insert_mode == "upsert",
          s_append.insert_mode)
    check("有物理主键 + 全量替换 → replace", s_replace.insert_mode == "replace",
          s_replace.insert_mode)
    check("无物理主键 → plain（与 DROP 开关无关）",
          s_plain.insert_mode == "plain" and
          build_sink(with_drop("F09633", 0)).insert_mode == "plain",
          s_plain.insert_mode)

    print("=== 2. SQL 文本由 TableSchema 唯一生成 ===")
    check("upsert 含 ON CONFLICT DO UPDATE",
          "ON CONFLICT(scene) DO UPDATE" in s_append._sql_for("T"))
    check("replace 用 INSERT OR REPLACE",
          s_replace._sql_for("T").startswith("INSERT OR REPLACE INTO T "))
    check("plain 是纯 INSERT（无 REPLACE / CONFLICT）",
          s_plain._sql_for("T").startswith("INSERT INTO T ") and
          "REPLACE" not in s_plain._sql_for("T") and
          "CONFLICT" not in s_plain._sql_for("T"))
    schema27 = TableSchema.from_profile(profiles["F09627"])
    check("insert_sql(T) 默认按表能力推导为 upsert",
          schema27.insert_sql("T") == s_append._sql_for("T"))
    schema33 = TableSchema.from_profile(profiles["F09633"])
    check("insert_sql(T) 无物理主键时默认 plain",
          schema33.insert_sql("T") == s_plain._sql_for("T"))
    check("显式 mode 与默认值一致（同一生成入口）",
          schema27.insert_sql("T", "upsert") == schema27.insert_sql("T") and
          schema33.insert_sql("T", "plain") == schema33.insert_sql("T"))

    print("=== 3. 非法组合显式报错，不静默生成 ===")
    check("无物理主键 + upsert 被拒",
          raises(lambda: schema33.insert_sql("T", "upsert")))
    check("无物理主键 + replace 被拒",
          raises(lambda: schema33.insert_sql("T", "replace")))
    check("未知 mode 被拒",
          raises(lambda: schema27.insert_sql("T", "bogus")))  # type: ignore[arg-type]
    check("INSERT_MODES 枚举固定", INSERT_MODES == ("plain", "replace", "upsert"),
          str(INSERT_MODES))

    print("=== 4. 装配层注入点仍可用（功能不变）===")
    forced = build_sink(with_drop("F09627", 0), insert_mode="replace")
    check("有物理主键的表可显式指定 replace",
          forced.insert_mode == "replace" and
          forced._sql_for("T").startswith("INSERT OR REPLACE INTO T "))
    check("无物理主键的表即使注入 upsert 仍归一化为 plain",
          build_sink(profiles["F09633"], insert_mode="upsert").insert_mode
          == "plain")
    check("非法 insert_mode 在装配层被拦",
          raises(lambda: build_sink(with_drop("F09627", 0),
                                    insert_mode="bogus")))

    print("=== 结论 ===")
    if _failures:
        print("FAILED: %d 项 → %s" % (len(_failures), _failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
