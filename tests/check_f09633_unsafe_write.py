"""F09633 临时吞吐开关自检：只关 F09633 的同步与回滚日志，且可一键恢复。

零第三方依赖、不连 SFTP / 在线服务；全部在临时目录跑，跑完即删。覆盖：

1. 缺省 / 开关 0 → 所有 AVL 的连接仍是 WAL + synchronous=NORMAL；
2. 开关 1 → 仅 F09633 改为 journal_mode=OFF + synchronous=OFF，其余 11 个
   AVL 不受影响；PRAGMA 条数契约（非 bulk 2 条 / bulk 5 条）与连接计数不变；
3. ``build_task()`` 把 profile 开关透传到 sink，且可显式注入覆盖；
4. 关闭日志下完整跑一次 atomic_swap（灌 stage → 建索引 → 切换 → 释放 old）
   仍成功，数据 / 水位 / 索引正确；
5. 改回 0 后连接恢复 WAL + NORMAL，库文件头也恢复 WAL 持久模式；
6. 非法值 fail-fast；不写该键（可选键）不破坏 68 个必需键契约。

用法：``python3 tests/check_f09633_unsafe_write.py``
"""
from __future__ import annotations

import shutil
import sqlite3 as sqlite3_mod
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sftp_avl2db.config as sftp_config                 # noqa: E402
from sftp_avl2db.config import load_profiles            # noqa: E402
from sftp_avl2db.profiles import build_task             # noqa: E402
from sftp_avl2db.sink import build_layout, build_sink   # noqa: E402
from sftp_avl2db.task_logging import InMemoryLogger     # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="f09633_unsafe_"))
ORIG_ENV = sftp_config.ENV_PATH
BASE_ENV = Path(ORIG_ENV).read_text(encoding="utf-8")
KEY = "F09633_SYNC_JOURNAL_OFF"
AVLS = ("F09554", "F09555", "F09556", "F09591",
        "F09627", "F09628", "F09629", "F09630",
        "F09631", "F09632", "F09633", "F09635")
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


def env_text(value):
    """value=None 表示整行删掉；否则把该键设为给定值。"""
    out = []
    for line in BASE_ENV.splitlines():
        if line.strip().startswith(KEY + "="):
            if value is not None:
                out.append(f"{KEY}={value}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def profiles_for(value, tag):
    p = TMP / ("env_" + tag)
    p.write_text(env_text(value), encoding="utf-8")
    sftp_config.ENV_PATH = p
    try:
        return load_profiles(runtime_dir=str(TMP / tag))
    finally:
        sftp_config.ENV_PATH = ORIG_ENV


def capture_pragmas(fn):
    """记录本次调用期间各 SQLite 连接实际执行的 PRAGMA 语句。"""
    calls = []
    orig = sqlite3_mod.connect

    def spy(*args, **kwargs):
        conn = orig(*args, **kwargs)
        conn.set_trace_callback(calls.append)
        return conn

    sqlite3_mod.connect = spy
    try:
        fn()
    finally:
        sqlite3_mod.connect = orig
    return [s for s in calls if s.upper().startswith("PRAGMA")]


def session_pragmas(sink, db_path, *, bulk=False):
    def go():
        with sink._session(db_path, bulk=bulk) as (conn, _):
            conn.commit()
    return capture_pragmas(go)


def has(pragmas, needle):
    return any(needle.upper() in p.upper() for p in pragmas)


def task_for(avl, tag, **kw):
    """每个 task 用独立 runtime 目录，避免共用单实例锁。"""
    prof = profiles_for(1, tag)[avl]
    return build_task(prof, logger=InMemoryLogger(),
                      sleeper=lambda _: None, **kw)


def main():
    print("=== 1. 缺省 / 开关 0：所有 AVL 仍是 WAL + NORMAL ===")
    p_absent = profiles_for(None, "absent")
    check("不写该可选键 → 全部 AVL 缺省 0",
          all(p.unsafe_no_durability == 0 for p in p_absent.values()),
          str({a: p_absent[a].unsafe_no_durability for a in AVLS}))
    p_off = profiles_for(0, "off")
    check("显式写 0 → 全部 AVL 为 0",
          all(p.unsafe_no_durability == 0 for p in p_off.values()))

    db = p_off["F09633"].db_dir / "probe_off.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    s_off = build_sink(p_off["F09633"])
    plain = session_pragmas(s_off, db)
    check("开关 0 非 bulk：PRAGMA 2 条且为 WAL + NORMAL",
          len(plain) == 2 and has(plain, "journal_mode = WAL")
          and has(plain, "synchronous = NORMAL")
          and not has(plain, "= OFF"), str(plain))
    check("开关 0 非 bulk 不累计 connections",
          s_off.stats.connections == 0, s_off.stats.connections)
    bulk = session_pragmas(s_off, db, bulk=True)
    check("开关 0 bulk：PRAGMA 5 条，仍为 WAL + NORMAL",
          len(bulk) == 5 and has(bulk, "journal_mode = WAL")
          and has(bulk, "synchronous = NORMAL")
          and has(bulk, "cache_size") and not has(bulk, "= OFF"), str(bulk))
    check("开关 0 bulk 累计 connections == 1",
          s_off.stats.connections == 1, s_off.stats.connections)

    print("=== 2. 开关 1：只命中 F09633，其余 11 个 AVL 不变 ===")
    p_on = profiles_for(1, "on")
    flags = {a: p_on[a].unsafe_no_durability for a in AVLS}
    check("仅 F09633 为 1，其余 11 个 AVL 仍为 0",
          flags["F09633"] == 1
          and all(v == 0 for a, v in flags.items() if a != "F09633"),
          str(flags))
    s_direct = build_sink(p_on["F09633"])
    check("build_sink 默认仍为安全口径（不隐式读 profile）",
          s_direct.unsafe_no_durability is False)

    s_unsafe = build_sink(p_on["F09633"], unsafe_no_durability=True,
                          atomic_swap=True)
    db2 = p_on["F09633"].db_dir / "probe_on.db"
    db2.parent.mkdir(parents=True, exist_ok=True)
    plain2 = session_pragmas(s_unsafe, db2)
    check("开关 1 非 bulk：PRAGMA 2 条且为 journal_mode=OFF + synchronous=OFF",
          len(plain2) == 2 and has(plain2, "journal_mode = OFF")
          and has(plain2, "synchronous = OFF")
          and not has(plain2, "= WAL"), str(plain2))
    bulk2 = session_pragmas(s_unsafe, db2, bulk=True)
    check("开关 1 bulk：PRAGMA 5 条，仍带 cache / temp_store / busy_timeout",
          len(bulk2) == 5 and has(bulk2, "journal_mode = OFF")
          and has(bulk2, "synchronous = OFF")
          and has(bulk2, "cache_size") and has(bulk2, "temp_store")
          and has(bulk2, "busy_timeout"), str(bulk2))

    s_other = build_sink(p_on["F09591"],
                         unsafe_no_durability=bool(
                             p_on["F09591"].unsafe_no_durability))
    db3 = p_on["F09591"].db_dir / "probe_other.db"
    db3.parent.mkdir(parents=True, exist_ok=True)
    other = session_pragmas(s_other, db3)
    check("F09591 在同一份 .env 下仍是 WAL + NORMAL",
          has(other, "journal_mode = WAL")
          and has(other, "synchronous = NORMAL")
          and not has(other, "= OFF"), str(other))

    print("=== 3. 装配层 build_task() 透传，且可显式覆盖 ===")
    t33 = task_for("F09633", "task33")
    check("build_task(F09633) 自动把开关透传到 sink",
          t33.sink.unsafe_no_durability is True)
    t33.close()
    t27 = task_for("F09627", "task27")
    check("build_task(F09627) 不受 F09633 开关影响",
          t27.sink.unsafe_no_durability is False)
    t27.close()
    t_override = task_for("F09633", "task_override",
                          unsafe_no_durability=False)
    check("显式注入 False 可覆盖配置（离线复跑安全回退）",
          t_override.sink.unsafe_no_durability is False)
    t_override.close()

    print("=== 4. 关闭日志下完整跑一次原子切换，功能不回归 ===")
    conf = profiles_for(1, "write_on")["F09633"]
    layout = build_layout(conf)
    sink = build_sink(conf, layout=layout, atomic_swap=True,
                      unsafe_no_durability=True)
    rows = [("2026-11-20", "u%d" % i, "1380000%04d" % i, "超套", "痛点%d" % i)
            for i in range(12)]
    n = sink.write("F0963320261120000000.AVL", rows)
    db4 = conf.db_dir / "F09633.db"
    conn = sqlite3_mod.connect(str(db4))
    try:
        # journal_mode=OFF 是连接级设置、不写进库文件头：裸连接看到的是
        # delete（默认），写入侧会话内才是 off。两者分别断言。
        disk_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        meta = conn.execute(
            "SELECT latest_file_date FROM _avl_meta WHERE avl='F09633'"
        ).fetchone()
        leftovers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'F09633_stage_%' "
            "OR name LIKE 'F09633_old_%'").fetchone()[0]
        all_rows = sum(
            conn.execute(f"SELECT COUNT(*) FROM {r[0]}").fetchone()[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'F09633_%' AND name NOT LIKE '%_stage_%' "
                "AND name NOT LIKE '%_old_%'"))
    finally:
        conn.close()
    check("原子切换写入 12 行且当前套总行数一致",
          n == 12 and all_rows == 12, f"{n}/{all_rows}")
    check("切换统计与水位正确、无 errors",
          sink.stats.swapped_tables == 128 and meta == ("20261120",)
          and leftovers == 0 and sink.stats.errors == [],
          f"{sink.stats.swapped_tables}/{meta}/{leftovers}/{sink.stats.errors}")
    check("OFF 是连接级设置、裸连接仍为 delete（不写进库文件头）",
          disk_mode == "delete", disk_mode)

    def probe_mode():
        with sink._session(db4, bulk=True) as (c, _):
            return str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    live_mode = probe_mode()
    check("写入侧会话内实际 journal_mode 为 off", live_mode == "off",
          live_mode)

    print("=== 5. 改回 0 并重启口径：库恢复 WAL + NORMAL ===")
    conf_restored = replace(conf, unsafe_no_durability=0)
    s_restored = build_sink(conf_restored, layout=layout,
                            atomic_swap=True, unsafe_no_durability=False)
    restored = session_pragmas(s_restored, db4)
    check("改回 0 后连接恢复 WAL + NORMAL",
          len(restored) == 2 and has(restored, "journal_mode = WAL")
          and has(restored, "synchronous = NORMAL"), str(restored))
    conn = sqlite3_mod.connect(str(db4))
    try:
        mode2 = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()
    check("库文件头恢复 WAL 持久模式", mode2 == "wal", mode2)

    print("=== 6. 非法值 fail-fast，且不破坏 68 必需键契约 ===")
    check("不写该键（可选键）→ load_profiles 仍产出 12 个 AVL",
          len(profiles_for(None, "absent2")) == 12)
    for bad in ("2", "-1", "yes", ""):
        tag = "bad_" + (bad or "empty")
        check(f"非法值 {bad!r} 被拦住",
              raises(lambda b=bad, t=tag: profiles_for(b, t)))

    print("=== 结论 ===")
    if _failures:
        print("FAILED: %d 项 → %s" % (len(_failures), _failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        sftp_config.ENV_PATH = ORIG_ENV
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(rc)
