"""runner 拆分与模式文案下沉自检（行为等价，零第三方依赖）。

不打任何服务、不连 SFTP。用真实 ``build_task()`` 装配，只把远端源换成内存替身，
断言：

1. 两份模式文案由 sink 提供，逐字符等于重构前 runner 里的原文；
2. runner 只决定「何时打印」，无该方法的 sink 替身走通用兜底且文案不变；
3. ``import_one()`` 拆成预检 / 取文件 / 解析入库后，MISSING / FAILED / OK /
   SKIP 四类结果与「写库成功才登记清单」的时机不变；
4. 控损 0 字节文件仍按「空文件(0 字节)」短路，不写库、不登记清单。

用法：``python3 tests/check_runner_refactor.py``
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sftp_avl2db.config import load_profiles                 # noqa: E402
from sftp_avl2db.profiles import build_task                  # noqa: E402
from sftp_avl2db.protocols import ImportStatus               # noqa: E402
from sftp_avl2db.sink import build_sink                     # noqa: E402
from sftp_avl2db.task_logging import InMemoryLogger         # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="runner_refactor_"))

DATE27, P27 = "20261120", "F09627"
NAME27 = f"{P27}{DATE27}000000.AVL"
DATE91, P91 = "20261125", "F09591"
NAME91 = f"{P91}{DATE91}000000.AVL"
DATE33, P33 = "20261120", "F09633"
NAME33 = f"{P33}{DATE33}000000.AVL"

# 两份模式文案的逐字符期望值，作为等价性基准
OLD_DROP_LOG = (f"[{P27}] 全量替换模式=1：{NAME27} 写入前 "
                "DROP 并重建全部目标表，只保留本批 AVL 数据；"
                "导入期间在线侧会读到空表（未启用双套表切换）")
OLD_SWAP_LOG = (f"[{P33}] 全量替换模式=1（双套表原子切换）："
                f"{NAME33} 先灌入 staging 套，全部完成后单事务 "
                "RENAME 切换，在线侧全程读当前套、无空窗")
OLD_STARTUP_DROP = "导入期间表为空、在线侧静默降级，请安排在业务低峰并公告窗口"
OLD_STARTUP_SWAP = ("已启用双套表原子切换：导入期间在线侧持续读上一日全量数据，"
                    "无空窗；切换事务为秒级，磁盘需预留两套数据空间")

_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' +
          (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


class FakeRemote:
    """只按文件名给内容的远端替身；stat 可注入异常。"""

    def __init__(self, files, *, stat_error=None):
        self.files = dict(files)
        self.stat_error = stat_error

    def connect(self):
        return None

    def exists(self, remote_path):
        if self.stat_error:
            raise RuntimeError(self.stat_error)
        return Path(remote_path).name in self.files

    def fetch(self, remote_path, local_path):
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(self.files[Path(remote_path).name], encoding="utf-8")

    def close(self):
        return None


def make_task(avl, files, tag, *, stat_error=None):
    profile = load_profiles(runtime_dir=str(TMP / tag))[avl]
    memo = InMemoryLogger()
    task = build_task(profile, logger=memo, sleeper=lambda _: None)
    task.runner.source = FakeRemote(files, stat_error=stat_error)
    task.sink.ensure_schema()
    return task, memo


def main():
    print("=== 1. 模式文案由 sink 逐字符提供 ===")
    profiles = load_profiles(runtime_dir=str(TMP / "texts"))
    s_drop = build_sink(profiles[P27])
    s_swap = build_sink(profiles[P33], atomic_swap=True)
    check("DROP 重建文案不变",
          s_drop.write_mode_log(NAME27, P27) == OLD_DROP_LOG,
          s_drop.write_mode_log(NAME27, P27))
    check("双套表切换文案不变",
          s_swap.write_mode_log(NAME33, P33) == OLD_SWAP_LOG,
          s_swap.write_mode_log(NAME33, P33))
    check("启动说明（DROP）不变",
          s_drop.startup_notice() == OLD_STARTUP_DROP)
    check("启动说明（切换）不变",
          s_swap.startup_notice() == OLD_STARTUP_SWAP)

    print("=== 2. runner 只决定何时打印，替身走通用兜底 ===")
    task27, _ = make_task(P27, {}, "r27")
    r27 = task27.runner
    check("runner 转发 sink 的 DROP 文案",
          r27._write_mode_log(NAME27, P27) == OLD_DROP_LOG)
    check("runner 转发 sink 的启动说明",
          r27._startup_mode_notice() == OLD_STARTUP_DROP)
    real_sink = r27.sink
    try:
        r27.sink = SimpleNamespace()          # 无 write_mode_log / startup_notice
        check("无该方法的 sink 替身 → 通用 DROP 兜底，文案仍不变",
              r27._write_mode_log(NAME27, P27) == OLD_DROP_LOG)
        check("无该方法的 sink 替身 → 通用启动兜底",
              r27._startup_mode_notice() == OLD_STARTUP_DROP)
    finally:
        r27.sink = real_sink

    task33, _ = make_task(P33, {}, "r33")
    r33 = task33.runner
    check("runner 转发 sink 的双套表文案",
          r33._write_mode_log(NAME33, P33) == OLD_SWAP_LOG)
    check("runner 转发 sink 的双套表启动说明",
          r33._startup_mode_notice() == OLD_STARTUP_SWAP)

    print("=== 3. import_one 四类结果与清单登记时机不变 ===")
    ok_task, memo = make_task(
        P27, {NAME27: "2026-11-20\t超套\t1\n"}, "ok")
    res = ok_task.runner.import_one(DATE27, P27)
    check("正常文件 → OK 且解析出 1 行",
          res.status == ImportStatus.OK and res.rows == 1,
          f"{res.status}/{res.rows}")
    infos = [x["message"] for x in memo.records if x["level"] == "INFO"]
    check("导入前打印的仍是原模式文案", OLD_DROP_LOG in infos, str(infos[-3:]))
    check("成功后登记 fetched 清单", ok_task.state.has(NAME27))
    res2 = ok_task.runner.import_one(DATE27, P27)
    check("二次调用 → SKIP「已在清单中」",
          res2.status == ImportStatus.SKIP and res2.detail == "已在清单中",
          f"{res2.status}/{res2.detail}")

    miss_task, _ = make_task(P27, {}, "miss")
    res3 = miss_task.runner.import_one(DATE27, P27)
    check("远程未生成 → MISSING",
          res3.status == ImportStatus.MISSING, res3.status)

    err_task, _ = make_task(P27, {}, "stat", stat_error="boom")
    res4 = err_task.runner.import_one(DATE27, P27)
    check("stat 异常 → FAILED「stat: boom」",
          res4.status == ImportStatus.FAILED and res4.detail == "stat: boom",
          f"{res4.status}/{res4.detail}")

    print("=== 4. 控损 0 字节短路，不写库不登记 ===")
    loss_task, _ = make_task(P91, {NAME91: ""}, "loss")
    res5 = loss_task.runner.import_one(DATE91, P91)
    check("0 字节 → FAILED「空文件(0 字节)」",
          res5.status == ImportStatus.FAILED
          and res5.detail == "空文件(0 字节)",
          f"{res5.status}/{res5.detail}")
    check("异常文件不登记清单", not loss_task.state.has(NAME91))

    print("=== 结论 ===")
    if _failures:
        print("FAILED: %d 项 → %s" % (len(_failures), _failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
