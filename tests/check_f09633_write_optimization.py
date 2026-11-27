"""F09633 写入优化自检：空表裸灌 + 灌完建索引 + 原子切换 + 两轮轮转。

不打任何服务、不连 SFTP。在临时目录跑完整 ``atomic_swap`` 流程，断言：
逻辑主键口径不变、物理 DDL 无 PRIMARY KEY、写入 SQL 为纯 INSERT、当前套带
非唯一 product_no 索引且在线查询命中它、写入侧零删行（上游全量 AVL 保证
(product_no, scene) 唯一，不做任何去重 / 全表扫描）、连续两轮切换索引名不冲突、
在线侧 reader 仍按 product_no 精确定位单张分表。

用法：``python3 tests/check_f09633_write_optimization.py``
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services import db_config, reader                        # noqa: E402
from sftp_avl2db.avl_specs import shard_index, table_name      # noqa: E402
from sftp_avl2db.config import load_profiles                   # noqa: E402
from sftp_avl2db.sink import build_layout, build_sink          # noqa: E402
from sftp_avl2db.sink.schema import TableSchema                # noqa: E402
from sftp_avl2db.table_schemas import (                        # noqa: E402
    TABLE_PRIMARY_KEYS, has_physical_primary_key)

TMP = Path(tempfile.mkdtemp(prefix="f09633_write_check_"))
FILES = ("F0963320261120000000.AVL", "F0963320261121000000.AVL")
TEL = "15054675009"

_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' +
          (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def rows_for(date, tag, *, duplicate=False):
    out = []
    for i in range(24):
        tel = "15%09d" % (i + 1)
        out.append((date, "U_%d" % i, tel, "超套", f"痛点{tag}-{i}"))
    # 上游保证 (product_no, scene) 唯一，正常数据不含重复。这里人为写一组
    # 同键行，用于验证「写入侧不做任何收敛」：两行都应原样入库。若上游违约，
    # 这就是 reader 层会拿到的形态（该情形按业务契约不应出现）；主链路对
    # pain_points 的兜底去重由 tests/check_pain_points_dedup.py 单独验证。
    out.append(("2026-11-20", "U_DUP", TEL, "流量不够用", f"痛点{tag}-旧"))
    if duplicate:
        out.append(("2026-11-20", "U_DUP", TEL, "流量不够用", f"痛点{tag}-新"))
    out.append(("2026-11-20", "U_TEL", TEL, "超套", f"痛点{tag}-主"))
    return out


def table_ddl(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return (row[0] or "") if row else ""


def indexes(conn, name):
    return [(r[1], r[2]) for r in conn.execute(f"PRAGMA index_list({name})")]


def current_table(conf, tel=TEL):
    return table_name("F09633", shard_index(tel, conf.num_tables))


def main():
    conf = load_profiles(runtime_dir=str(TMP))["F09633"]
    layout = build_layout(conf)
    db = conf.db_dir / "F09633.db"
    schema = TableSchema.from_profile(conf)

    print("=== 1. 逻辑主键 / 物理 DDL / 写入 SQL ===")
    check("conf_user_scene 仍注册逻辑主键 (product_no, scene)",
          TABLE_PRIMARY_KEYS["conf_user_scene"] == ("product_no", "scene") and
          conf.primary_key == ("product_no", "scene"))
    check("conf_user_scene 声明为不建物理主键",
          conf.physical_pk is False and
          has_physical_primary_key("conf_user_scene") is False and
          has_physical_primary_key("loss_control") is True)
    ddl = schema.create_table_sql("F09633_stage_0")
    check("stage 建表 DDL 不含 PRIMARY KEY",
          "PRIMARY KEY" not in ddl, ddl)
    check("逻辑主键列仍自动补 NOT NULL",
          "product_no VARCHAR(50) NOT NULL" in ddl and
          "scene VARCHAR(50) NOT NULL" in ddl)
    check("非 DROP 模式下 insert_sql 为纯 INSERT（无 ON CONFLICT）",
          "ON CONFLICT" not in schema.insert_sql("F09633_stage_0"))

    sink = build_sink(conf, layout=layout, atomic_swap=True)
    check("DROP 模式下写入 SQL 为纯 INSERT",
          sink._sql_for("F09633_stage_0").startswith("INSERT INTO ") and
          "REPLACE" not in sink._sql_for("F09633_stage_0") and
          "CONFLICT" not in sink._sql_for("F09633_stage_0"),
          sink._sql_for("F09633_stage_0"))

    print("=== 2. 第一轮：灌 stage → 建索引 → 原子切换 ===")
    n = sink.write(FILES[0], rows_for("2026-11-20", "A", duplicate=True), layout)
    check("写入行数正确", n == 27, f"{n} 行")
    check("128 张 stage 表各建 1 个查询索引",
          sink.stats.stage_indexes == conf.num_tables,
          str(sink.stats.stage_indexes))
    check("128 组 RENAME 在同一事务提交",
          sink.stats.swapped_tables == conf.num_tables,
          str(sink.stats.swapped_tables))

    conn = sqlite3.connect(str(db))
    try:
        cur_tbl = current_table(conf)
        check("首轮切换后当前套存在", cur_tbl in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})
        check("当前套 DDL 不含 PRIMARY KEY",
              "PRIMARY KEY" not in table_ddl(conn, cur_tbl))
        idx = indexes(conn, cur_tbl)
        check("当前套只有 1 个非唯一索引",
              len(idx) == 1 and idx[0][1] == 0, str(idx))
        idx_name = idx[0][0]
        check("索引列为 product_no",
              [r[2] for r in conn.execute(
                  f"PRAGMA index_info({idx_name})")] == ["product_no"])
        plan = conn.execute(
            f"EXPLAIN QUERY PLAN SELECT * FROM {cur_tbl} "
            "WHERE product_no = ? ORDER BY op_time DESC", (TEL,)).fetchall()
        check("在线查询命中 product_no 索引",
              any(idx_name in str(r) for r in plan), str(plan))
        dup = conn.execute(
            f"SELECT reason FROM {cur_tbl} "
            "WHERE product_no = ? AND scene = '流量不够用'", (TEL,)).fetchall()
        check("写入侧不做收敛：同键两行原样入库（上游违约时的形态）",
              {r[0] for r in dup} == {"痛点A-旧", "痛点A-新"}
              and len(dup) == 2, str(dup))
        all_rows = sum(
            conn.execute(f"SELECT COUNT(*) FROM {r[0]}").fetchone()[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'F09633_%' AND name NOT LIKE '%_stage_%' "
                "AND name NOT LIKE '%_old_%'"))
        check("写入侧零删行：当前套总行数 == 写入行数",
              all_rows == n, f"入库 {all_rows} / 写入 {n}")
        meta = conn.execute(
            "SELECT latest_file_date FROM _avl_meta WHERE avl='F09633'").fetchone()
        check("切换事务内推进 _avl_meta 水位",
              meta == ("20261120",), str(meta))
        leftovers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'F09633_stage_%' "
            "OR name LIKE 'F09633_old_%'").fetchone()[0]
        check("stage / old 套已释放", leftovers == 0, str(leftovers))
    finally:
        conn.close()

    print("=== 3. 在线侧 reader 按 product_no 精确定位 ===")
    read_config = db_config.load(
        avl_db_root=TMP / "sqlite_db",
        dimensions_db=TMP / "dimensions.db",
    )
    reader.configure(read_config)
    db_config.validate(read_config)
    hit = asyncio.run(reader.query_many("F09633", where={"product_no": TEL}))
    reasons = {r["reason"] for r in hit}
    check("reader 取到该号码的全部场景行（写入侧未删行）",
          reasons == {"痛点A-旧", "痛点A-新", "痛点A-主"}
          and len(hit) == 3, f"{len(hit)} 行 {reasons}")
    check("缺 product_no 的分表查询降级为 []",
          asyncio.run(reader.query_many(
              "F09633", where={"scene": "超套"})) == [])

    print("=== 4. 第二轮切换：索引名不撞、数据全量替换 ===")
    sink.write(FILES[1], rows_for("2026-11-21", "B"), layout)
    conn = sqlite3.connect(str(db))
    try:
        cur_tbl = current_table(conf)
        idx = indexes(conn, cur_tbl)
        check("第二轮后当前套仍只有 1 个非唯一索引",
              len(idx) == 1 and idx[0][1] == 0, str(idx))
        check("索引名按轮次更新（未与 old 套撞名）",
              idx[0][0] != idx_name, f"{idx_name} → {idx[0][0]}")
        total = conn.execute(
            f"SELECT COUNT(*) FROM {cur_tbl}").fetchone()[0]
        fresh = conn.execute(
            f"SELECT COUNT(*) FROM {cur_tbl} WHERE reason LIKE '痛点B%'"
        ).fetchone()[0]
        check("全量替换后该分表只剩第二批数据",
              total > 0 and total == fresh, f"总 {total} / 第二批 {fresh}")
        check("上一批数据不残留", total - fresh == 0, str(total - fresh))
    finally:
        conn.close()

    print("=== 结论 ===")
    if _failures:
        print("FAILED: %d 项 → %s" % (len(_failures), _failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
