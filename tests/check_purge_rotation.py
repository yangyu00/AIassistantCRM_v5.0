"""库表清理轮转自检：验证「每天一次 + 按天清部分分表」的覆盖性。"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sftp_avl2db.config as cfg                       # noqa: E402
from sftp_avl2db.config import (                       # noqa: E402
    _int, _num_tables, load_profiles)
from sftp_avl2db.runner import Avl2DbRunner             # noqa: E402
from sftp_avl2db.sink import build_layout, build_sink  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="purge_rotation_"))
_failures = []
DAY0 = datetime(2026, 11, 21, 10, 0, 0)


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f' → {detail}' if detail else ""))
    if not ok:
        _failures.append(label)


def raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


class Log:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(msg)

    def error(self, msg):
        self.lines.append(msg)


class SpySink:
    """包住真实 SqliteSink，记录 purge_expired 的调用次数与参数。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    @property
    def stats(self):
        return self.inner.stats

    def ensure_schema(self):
        return self.inner.ensure_schema()

    def purge_expired(self, cutoff, tables=None):
        self.calls.append((cutoff, None if tables is None else list(tables)))
        return self.inner.purge_expired(cutoff, tables)


def make_profile(tag, *, num_tables=128, cycle=7, sharded=True):
    """构造最小可用的 profile，不读 .env、不改分表映射。"""
    base = TMP / tag
    return SimpleNamespace(
        name="FTEST",
        project="testproj",
        db_dir=base / "sqlite_db" / "testproj",
        local_dir=base / "files" / "testproj",
        sharded=sharded,
        num_tables=num_tables,
        file_prefixes=("FTEST",),
        schema=(("op_time", "TEXT"),
                ("user_id", "TEXT"),
                ("product_no", "TEXT NOT NULL")),
        primary_key=("product_no",),
        time_column="op_time",
        retention=SimpleNamespace(
            batch_size=1000, data_days=3, local_file_days=3,
            poll_seconds=1800, purge_cycle_days=cycle),
    )


def seed(profile, layout):
    """每张表插 1 行过期 + 1 行新鲜。"""
    sink = build_sink(profile, layout=layout)
    sink.ensure_schema()
    conn = sqlite3.connect(str(layout.db_path))
    try:
        for _, table in layout.all_targets():
            conn.execute(
                f"INSERT OR REPLACE INTO {table} "
                "(op_time, user_id, product_no) VALUES (?, ?, ?)",
                ("2020-01-01", "expired", f"e-{table}"))
            conn.execute(
                f"INSERT OR REPLACE INTO {table} "
                "(op_time, user_id, product_no) VALUES (?, ?, ?)",
                ("2099-01-01", "fresh", f"f-{table}"))
        conn.commit()
    finally:
        conn.close()


def count_rows(db_path, tables, user_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return sum(
            conn.execute(
                f"SELECT count(*) FROM {t} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            for t in tables)
    finally:
        conn.close()


def make_runner(profile, cycle=7):
    profile.retention.purge_cycle_days = cycle
    profile.local_dir.mkdir(parents=True, exist_ok=True)
    layout = build_layout(profile)
    inner = build_sink(profile, layout=layout)
    spy = SpySink(inner)
    clock = {"now": DAY0}
    runner = Avl2DbRunner(
        profile, source=None, parser=None, sink=spy, layout=layout,
        state=None, lock=None, log=Log(), sleeper=lambda _: None,
        now=lambda: clock["now"])
    return runner, spy, layout, clock


print("[1] 配置解析：AVL 粒度、必需键、无项目级兜底")
# _int 默认要求正整数；min_value=0 供 PURGE_CYCLE_DAYS 使用（0 合法，但不是缺省值）
check("正整数合法", _int({"X": "7"}, "X") == 7)
check("正整数：缺键 fail-fast", raises(lambda: _int({}, "X")))
check("正整数：0 fail-fast", raises(lambda: _int({"X": "0"}, "X")))
check("正整数：负数 fail-fast", raises(lambda: _int({"X": "-1"}, "X")))
check("正整数：非数字 fail-fast", raises(lambda: _int({"X": "abc"}, "X")))
check("周期键：0 合法", _int({"X": "0"}, "X", min_value=0) == 0)
check("周期键：正数合法", _int({"X": "7"}, "X", min_value=0) == 7)
check("周期键：缺键仍 fail-fast（无缺省回落）",
      raises(lambda: _int({}, "X", min_value=0)))
check("周期键：负数 fail-fast",
      raises(lambda: _int({"X": "-1"}, "X", min_value=0)))
# _num_tables 只认 <AVL>_NUM_TABLES，项目级键一律不被读取
check("分表数：按 AVL 名直读",
      _num_tables({"FTEST_NUM_TABLES": "128"}, "FTEST") == 128)
check("分表数：项目级键不构成兜底",
      raises(lambda: _num_tables({"TESTPROJ_NUM_TABLES": "128"}, "FTEST")))
check("分表数：缺 AVL 键 fail-fast", raises(lambda: _num_tables({}, "FTEST")))
check("分表数：非法值 fail-fast",
      raises(lambda: _num_tables({"FTEST_NUM_TABLES": "0"}, "FTEST")))

print("[2] 轮转分组：7 组覆盖 128 张表且互不重叠")
r0, spy0, layout0, _ = make_runner(make_profile("plan"))
all_tables = [t for _, t in layout0.all_targets()]
base_ord = DAY0.date().toordinal()
groups = []
sizes = []
for d in range(7):
    picked, total, effective = r0._purge_plan(base_ord + d, 7)
    groups.append(set(picked))
    sizes.append(len(picked))
check("总表数 128", len(all_tables) == 128 and total == 128)
check("生效周期 7", effective == 7)
check("并集覆盖全部分表", set().union(*groups) == set(all_tables))
check("组间无交集",
      all(not (groups[i] & groups[j])
          for i in range(7) for j in range(i + 1, 7)))
check("各组数量均衡(差 ≤ 1) 且合计 128",
      max(sizes) - min(sizes) <= 1 and sum(sizes) == 128,
      f"各组大小={sizes}")
check("周期大于表数时收敛", r0._purge_plan(base_ord, 999)[2] == 128)

print("[3] cycle=0：每轮全量、无日历闸门")
r_old, spy_old, layout_old, clock_old = make_runner(
    make_profile("case_cycle0", cycle=0), cycle=0)
seed(r_old.profile, layout_old)
for _ in range(3):
    r_old.cleanup_expired()
check("调用 3 次", len(spy_old.calls) == 3)
check("每次都全量(tables=None)",
      all(t is None for _, t in spy_old.calls))
check("同日不被闸门拦住", r_old.cleanup_expired()["purge_skipped"] is False)

print("[4] 固定表不参与轮转，仍每天全清一次")
r_flat, spy_flat, layout_flat, clock_flat = make_runner(
    make_profile("flat", num_tables=0, sharded=False, cycle=7), cycle=7)
flat_tables, flat_total, _ = r_flat._purge_plan(base_ord, 7)
check("固定表当天全清", flat_tables == ["FTEST"] and flat_total == 1)
seed(r_flat.profile, layout_flat)
for d in range(3):
    clock_flat["now"] = DAY0 + timedelta(days=d)
    r_flat.cleanup_expired()
    r_flat.cleanup_expired()               # 同日第二轮仍应跳过
check("固定表连续 3 天每天仍全清一次",
      len(spy_flat.calls) == 3
      and all(ts == ["FTEST"] for _, ts in spy_flat.calls))
check("固定表过期行已清", count_rows(layout_flat.db_path, ["FTEST"], "expired") == 0)

print("[5] 端到端：7 天轮完，过期行全清、新鲜行保留")
prof = make_profile("rotate")
r5, spy5, layout5, clock5 = make_runner(prof)
seed(prof, layout5)
tables5 = [t for _, t in layout5.all_targets()]
for d in range(7):
    clock5["now"] = DAY0 + timedelta(days=d)
    first = r5.cleanup_expired()
    second = r5.cleanup_expired()          # 同日第二轮：应跳过库表清理
    check(f"第 {d + 1} 天只清一次", second["purge_skipped"] is True
          and first["purge_skipped"] is False)
check("7 天共 7 次 purge_expired()", len(spy5.calls) == 7)
picked_all = [t for _, ts in spy5.calls for t in ts]
check("7 次合计恰好 128 张表", len(picked_all) == 128)
check("无表被重复清", len(set(picked_all)) == 128)
check("过期行已全部删除",
      count_rows(layout5.db_path, tables5, "expired") == 0)
check("新鲜行全部保留",
      count_rows(layout5.db_path, tables5, "fresh") == 128)
check("清理无异常", spy5.stats.errors == []
      and not any("清理过期数据失败" in m for m in r5.log.lines))

print("[6] 单次 --date 补跑：一次全量清，不参与轮转")
prof6 = make_profile("full")
r6, spy6, layout6, _ = make_runner(prof6)
seed(prof6, layout6)
tables6 = [t for _, t in layout6.all_targets()]
res6 = r6.cleanup_expired(force_full=True)
check("只调用一次", len(spy6.calls) == 1)
check("全量(tables=None)", spy6.calls[0][1] is None)
check("128 张表的过期行一次清完",
      count_rows(layout6.db_path, tables6, "expired") == 0)
check("新鲜行保留", count_rows(layout6.db_path, tables6, "fresh") == 128)
check("返回本次清表数", res6["purged_tables"] == 128)

print("[7] 真实 load_profiles() 按 AVL 装配全部参数")
ENV = ROOT / "sftp_avl2db" / ".env"
_created_env = not ENV.is_file()
if _created_env:
    shutil.copyfile(ENV.with_name(".env.example"), ENV)
try:
    profiles = load_profiles(runtime_dir=str(TMP / "profiles"))
    check("12 个 AVL 各自一份 profile", len(profiles) == 12)
    check("全部 profile 的 5 个运行参数均合法",
          all(p.retention.batch_size > 0
              and p.retention.data_days > 0
              and p.retention.local_file_days > 0
              and p.retention.poll_seconds > 0
              and isinstance(p.retention.purge_cycle_days, int)
              and p.retention.purge_cycle_days >= 0
              for p in profiles.values()))
    check("只有 2 个分表 AVL 有 num_tables",
          {n: p.num_tables for n, p in profiles.items()
           if p.num_tables} == {"F09591": 128, "F09633": 128})
    if _created_env:
        check("模板里每个 AVL 的清理周期独立取值",
              profiles["F09554"].retention.purge_cycle_days == 1
              and profiles["F09591"].retention.purge_cycle_days == 2
              and profiles["F09627"].retention.purge_cycle_days == 1
              and profiles["F09633"].retention.purge_cycle_days == 7)
        check("模板里轮询间隔按 AVL 粒度生效",
              profiles["F09554"].retention.poll_seconds == 600
              and profiles["F09627"].retention.poll_seconds == 1800)
    tmpl_keys = [
        l.split("=", 1)[0].strip()
        for l in ENV.with_name(".env.example").read_text(
            encoding="utf-8").splitlines()
        if l.strip() and not l.strip().startswith("#") and "=" in l]
    check("模板共 67 个键", len(tmpl_keys) == 67)
    check("模板无任何项目级键前缀",
          not any(k.startswith(("OPPORTUNITY_RECALL_", "LOSS_CONTROL_",
                                "DIAGNOSIS_")) for k in tmpl_keys))
finally:
    if _created_env:
        ENV.unlink()

print("[8] 项目级键不构成兜底 / 每个 AVL 缺键 fail-fast / 取值互不串")
TEMPLATE = (ROOT / "sftp_avl2db" / ".env.example").read_text(
    encoding="utf-8").splitlines()


def env_lines(drop=(), override=None):
    """从模板生成一份临时 env：可删键、可改值。"""
    override = override or {}
    out = []
    for line in TEMPLATE:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key = s.split("=", 1)[0].strip()
        if key in drop:
            continue
        out.append(f"{key}={override[key]}" if key in override else s)
    return out


def load_with(lines):
    """把 .env 指向临时文件跑一次 load_profiles()，用完立刻还原。"""
    path = TMP / "case.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    old = cfg.ENV_PATH
    cfg.ENV_PATH = path
    try:
        return load_profiles(runtime_dir=str(TMP / "case_profiles"))
    finally:
        cfg.ENV_PATH = old


check("完整模板可装配", len(load_with(env_lines())) == 12)
check("删 F09591_NUM_TABLES → fail-fast",
      raises(lambda: load_with(env_lines(drop=("F09591_NUM_TABLES",)))))
check("删 F09591_NUM_TABLES 且另加无关键 → 仍 fail-fast",
      raises(lambda: load_with(env_lines(drop=("F09591_NUM_TABLES",))
                               + ["LOSS_CONTROL_NUM_TABLES=128"])))
check("删 F09627_POLL_INTERVAL_SECONDS → fail-fast（不复用同项目其它 AVL）",
      raises(lambda: load_with(
          env_lines(drop=("F09627_POLL_INTERVAL_SECONDS",))
          + ["DIAGNOSIS_POLL_INTERVAL_SECONDS=1800"])))
check("删 F09633_PURGE_CYCLE_DAYS → fail-fast（无缺省 0）",
      raises(lambda: load_with(
          env_lines(drop=("F09633_PURGE_CYCLE_DAYS",))
          + ["DIAGNOSIS_PURGE_CYCLE_DAYS=7"])))
_case = load_with(env_lines(
    override={"F09554_POLL_INTERVAL_SECONDS": "123",
              "F09554_PURGE_CYCLE_DAYS": "3"}))
check("改单个 AVL 的轮询间隔不影响同项目其它 AVL",
      _case["F09554"].retention.poll_seconds == 123
      and _case["F09555"].retention.poll_seconds == 600)
check("改单个 AVL 的清理周期不影响同项目其它 AVL",
      _case["F09554"].retention.purge_cycle_days == 3
      and _case["F09556"].retention.purge_cycle_days == 1)

shutil.rmtree(TMP, ignore_errors=True)
print()
if _failures:
    print(f"FAILED: {len(_failures)} 项 → {', '.join(_failures)}")
    sys.exit(1)
print("ALL PASS")
