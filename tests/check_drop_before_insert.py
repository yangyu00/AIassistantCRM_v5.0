"""配置变更自检：按业务新鲜度窗口 / 诊断 365 天 / 全量替换模式。

零第三方依赖，不连 SFTP / IOP / 在线服务；全部落在临时目录，跑完即删。
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sftp_avl2db.config as sftp_config          # noqa: E402
from sftp_avl2db.config import load_profiles      # noqa: E402
from sftp_avl2db.sink import build_sink           # noqa: E402
from services import db_config, reader            # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="check_modes_"))
DIAG_AVLS = ("F09627", "F09628", "F09629", "F09630",
             "F09631", "F09632", "F09633", "F09635")
OTHER_AVLS = ("F09554", "F09555", "F09556", "F09591")
_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


def rows_of(db_path, table, cols="*"):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT {cols} FROM {table}").fetchall()
    finally:
        conn.close()


def main():
    print("=== 1. sftp_avl2db：诊断 8 张表保留 365 天 ===")
    profiles = load_profiles(runtime_dir=str(TMP))
    check("诊断 8 张表 DATA_EXPIRE_DAYS 全为 365",
          all(profiles[a].retention.data_days == 365 for a in DIAG_AVLS),
          str({a: profiles[a].retention.data_days for a in DIAG_AVLS}))
    check("商机 3 张 + 控损 1 张仍为 3",
          all(profiles[a].retention.data_days == 3 for a in OTHER_AVLS))
    check("全局模式开关默认关闭（0）",
          all(p.retention.drop_before_insert == 0 for p in profiles.values()))

    print("=== 2. sftp_avl2db：模式开关的读取 / 覆盖 / 校验 ===")
    orig_env = sftp_config.ENV_PATH
    env_text = Path(orig_env).read_text(encoding="utf-8")
    override_env = TMP / "env_override"
    override_env.write_text(
        env_text.replace("AVL_DROP_BEFORE_INSERT=0",
                         "AVL_DROP_BEFORE_INSERT=0\n"
                         "F09627_DROP_BEFORE_INSERT=1\n"
                         "F09633_DROP_BEFORE_INSERT=1"),
        encoding="utf-8")
    try:
        sftp_config.ENV_PATH = override_env
        p2 = load_profiles(runtime_dir=str(TMP))
        check("按 AVL 覆盖：F09627 / F09633 开启，其余仍关闭",
              p2["F09627"].retention.drop_before_insert == 1
              and p2["F09633"].retention.drop_before_insert == 1
              and p2["F09554"].retention.drop_before_insert == 0)
        bad_env = TMP / "env_bad"
        bad_env.write_text(
            env_text.replace("AVL_DROP_BEFORE_INSERT=0",
                             "AVL_DROP_BEFORE_INSERT=2"), encoding="utf-8")
        sftp_config.ENV_PATH = bad_env
        check("模式开关只认 0/1，非法值 2 被拦住",
              raises(lambda: load_profiles(runtime_dir=str(TMP))))
    finally:
        sftp_config.ENV_PATH = orig_env

    print("=== 3. services：新鲜度窗口按业务分别生效 ===")
    cfg = db_config.load()
    check("商机 2 / 控损 2 / 诊断 0",
          cfg.freshness_window_days_for("opportunity_recall") == 2
          and cfg.freshness_window_days_for("loss_control") == 2
          and cfg.freshness_window_days_for("diagnosis") == 0,
          str(dict(cfg.freshness_window_by_project)))
    check("旧全局键常量已从 db_config 移除",
          not hasattr(db_config, "FRESHNESS_WINDOW_ENV_KEY"))
    legacy_env = TMP / "legacy.env"
    legacy_env.write_text(
        (ROOT / "services" / ".env").read_text(encoding="utf-8")
        + "\nAVL_FRESHNESS_WINDOW_DAYS=7\n", encoding="utf-8")
    cfg_legacy = db_config.load(env_path=legacy_env)
    check(".env 里残留旧全局键 =7 也不生效（不覆盖三项）",
          cfg_legacy.freshness_window_days_for("opportunity_recall") == 2
          and cfg_legacy.freshness_window_days_for("loss_control") == 2
          and cfg_legacy.freshness_window_days_for("diagnosis") == 0,
          str(dict(cfg_legacy.freshness_window_by_project)))
    check("AVL_DB_SPECS 归属正确",
          cfg.profiles["F09554"].project == "opportunity_recall"
          and cfg.profiles["F09591"].project == "loss_control"
          and all(cfg.profiles[a].project == "diagnosis" for a in DIAG_AVLS))
    reader.configure(cfg)
    check("_cutoff 按业务取值：商机 N=2 下界 18 日",
          reader._cutoff("2026-11-19", "opportunity_recall") == "2026-11-18")
    check("_cutoff 按业务取值：诊断 N=0 关闭",
          reader._cutoff("2026-11-19", "diagnosis") is None)
    check("_cutoff 不传业务时用全局回退值（离线自检口径）",
          reader._cutoff("2026-11-19") == "2026-11-18")

    # 端到端：诊断 AVL 里造 3 个批次，窗口 0 不过滤；单值注入 2 则收敛到最新批
    diag_db = TMP / "sqlite_db" / "diagnosis" / "F09627.db"
    diag_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(diag_db))
    try:
        conn.execute("CREATE TABLE F09627 (op_time TEXT, scene TEXT, rn INTEGER, "
                     "PRIMARY KEY (scene))")
        conn.execute("CREATE TABLE _avl_meta (avl TEXT PRIMARY KEY, "
                     "latest_file_date TEXT NOT NULL, latest_op_time TEXT NOT NULL, "
                     "last_filename TEXT NOT NULL, updated_at TEXT NOT NULL)")
        for day, scene in (("2026-11-19", "新批"), ("2026-11-18", "次新批"),
                           ("2026-11-17", "过时批")):
            conn.execute("INSERT INTO F09627 VALUES (?, ?, 1)", (day, scene))
        conn.execute("INSERT INTO _avl_meta VALUES (?, ?, ?, ?, ?)",
                     ("F09627", "20261120", "2026-11-19", "x.AVL", "now"))
        conn.commit()
    finally:
        conn.close()

    cfg0 = db_config.load(avl_db_root=TMP / "sqlite_db",
                          dimensions_db=TMP / "dim.db")
    reader.configure(cfg0)
    many0 = asyncio.run(reader.query_many(
        "F09627", where={"scene": ["新批", "次新批", "过时批"]}))
    check("诊断 N=0 → 查询不做批次收敛，3 批全返回",
          len(many0) == 3, str(sorted(r["op_time"] for r in many0)))
    cfg2 = db_config.load(avl_db_root=TMP / "sqlite_db",
                          dimensions_db=TMP / "dim.db",
                          freshness_window_days=2)
    reader.configure(cfg2)
    many2 = asyncio.run(reader.query_many(
        "F09627", where={"scene": ["新批", "次新批", "过时批"]}))
    check("单值注入 2 → 全部业务同值，收敛到 19 日最新批",
          bool(many2) and all(r["op_time"] == "2026-11-19" for r in many2),
          str(sorted({r["op_time"] for r in many2})))
    check("负窗口仍被拦截",
          raises(lambda: db_config.load(avl_db_root=TMP / "sqlite_db",
                                        freshness_window_days=-1)))
    reader.configure(db_config.load())

    print("=== 4. 全量替换模式：关闭时追加、开启时只留最新批 ===")
    # 独立 runtime 目录：第 3 节在 TMP/sqlite_db 下造过 F09627.db，
    # 这里另起一份，避免两节共用同一个 db 文件互相污染。
    profiles_modes = load_profiles(runtime_dir=str(TMP / "modes"))
    p27 = profiles_modes["F09627"]
    db27 = p27.db_dir / "F09627.db"
    sink_off = build_sink(p27)
    sink_off.ensure_schema()
    sink_off.write("F0962720261110000000.AVL",
                   [("2026-11-09", "旧场景", 1)])
    sink_off.write("F0962720261120000000.AVL",
                   [("2026-11-19", "新场景", 2)])
    check("模式 0：两批按主键追加，旧场景仍在",
          len(rows_of(db27, "F09627")) == 2
          and {r[1] for r in rows_of(db27, "F09627")} == {"旧场景", "新场景"},
          str(rows_of(db27, "F09627")))

    p27_on = replace(p27, retention=replace(p27.retention,
                                            drop_before_insert=1))
    sink_on = build_sink(p27_on)
    sink_on.write("F0962720261121000000.AVL",
                  [("2026-11-20", "最新场景", 3)])
    after = rows_of(db27, "F09627")
    check("模式 1：写入前 DROP 重建，只留最新批一行",
          len(after) == 1 and after[0][1] == "最新场景", str(after))
    check("stats.dropped_tables = 1（固定表）",
          sink_on.stats.dropped_tables == 1, str(sink_on.stats.dropped_tables))
    meta = rows_of(db27, "_avl_meta", "avl, latest_file_date")
    check("_avl_meta 不随业务表被 DROP，水位推进到 20261121",
          len(meta) == 1 and meta[0][1] == "20261121", str(meta))

    print("=== 5. 全量替换模式：分表 AVL 全部 128 张表重建 ===")
    p33 = profiles_modes["F09633"]
    db33 = p33.db_dir / "F09633.db"
    sink33_off = build_sink(p33)
    sink33_off.ensure_schema()
    tables33 = [t for _, t in sink33_off.layout.all_targets()]
    conn = sqlite3.connect(str(db33))
    try:
        for t in tables33:
            conn.execute(
                f"INSERT OR REPLACE INTO {t} "
                "(op_time, user_id, product_no, scene, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                ("2020-01-01", "old", "13800000000", "旧场景", "旧原因"))
        conn.commit()
    finally:
        conn.close()
    check("128 张分表先各自塞入 1 行旧数据",
          len(tables33) == 128
          and sum(len(rows_of(db33, t)) for t in tables33) == 128)

    p33_on = replace(p33, retention=replace(p33.retention,
                                            drop_before_insert=1))
    sink33_on = build_sink(p33_on)
    sink33_on.write("F0963320261121000000.AVL",
                    [("2026-11-20", "u", "13800000000", "新场景", "新原因")])
    total_rows = sum(len(rows_of(db33, t)) for t in tables33)
    old_rows = sum(
        len([r for r in rows_of(db33, t) if r[1] == "old"]) for t in tables33)
    check("模式 1：128 张表全部重建，旧数据清零",
          old_rows == 0, f"残留旧行={old_rows}")
    check("模式 1：分表库只剩本批 1 行",
          total_rows == 1, f"总行数={total_rows}")
    check("stats.dropped_tables = 128（全部分表）",
          sink33_on.stats.dropped_tables == 128,
          str(sink33_on.stats.dropped_tables))

    return not _failures


if __name__ == "__main__":
    try:
        passed = main()
    finally:
        try:
            reader.configure(db_config.load())
        except Exception:
            pass
        sftp_config.ENV_PATH = Path(__file__).resolve().parents[1] / \
            "sftp_avl2db" / ".env"
        shutil.rmtree(TMP, ignore_errors=True)
    print("=== ALL PASS ===" if passed else
          f"=== FAILED: {len(_failures)} 项 → {_failures} ===")
    sys.exit(0 if passed else 1)
