# -*- coding: utf-8 -*-
"""阶段 1 校验：配置层（标准库 unittest，零新增依赖）。

覆盖：
1. ``import sftp_avl2db`` / ``import crm_dbspec`` 零副作用（独立子进程验证）；
2. ``load_profiles()`` 与冻结快照 ``tests/fixtures/legacy_specs.json``
   逐字段一致（阶段 5 删除旧 ``tasks/config.py`` 后改用快照继续锁定配置不漂移）；
3. dotenv 解析逐字一致（直接对比旧函数输出）；
4. 环境变量优先级与 TASKS_ENV_FILE 覆盖；
5. ``validate_specs()`` 能复现原有全部配置漂移校验；
6. ``from_mapping()`` 在完全没有 .env 的情况下可用（其他项目复用）；
7. 默认路径沿用现有 ``tasks/log|state|files|sqlite_db`` 约定。

运行（仓库根目录）：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm_dbspec                                    # noqa: E402
from sftp_avl2db import (                            # noqa: E402
    TaskProfileConfig, iter_remote_names, load_profiles,
    load_values, parse_dotenv,
)

# 旧 tasks/config.py 在阶段 5 已删除；这里改用生成时的冻结快照，
# 保证「配置逐字段不漂移」的回归能力仍然成立，且套件不依赖已删模块。
LEGACY_SPECS = json.loads(
    (ROOT / "tests" / "fixtures" / "legacy_specs.json").read_text(encoding="utf-8"))

SENSITIVE_PREFIXES = ("SFTP_", "OPPORTUNITY_RECALL_", "LOSS_CONTROL_", "TASKS_ENV_FILE")


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith(SENSITIVE_PREFIXES)}


class Stage1Tests(unittest.TestCase):

    def setUp(self):
        self.profiles = load_profiles(env_path=ROOT / ".env", project_root=ROOT)

    # ---- 1. import 零副作用 ----
    def test_import_has_no_side_effects(self):
        code = r'''
import builtins, os, sys
from pathlib import Path
repo = sys.argv[1]
sys.path.insert(0, repo)
os.environ.clear()
os.environ.update(__import__("json").loads(sys.argv[2]))
os.environ["TASKS_ENV_FILE"] = "/nonexistent/no-such-file.env"

real_open = builtins.open
opened = []
def tracking_open(file, *a, **kw):
    opened.append(str(file))
    return real_open(file, *a, **kw)
builtins.open = tracking_open
real_read = Path.read_text
def tracking_read(self, *a, **kw):
    opened.append(str(self))
    return real_read(self, *a, **kw)
Path.read_text = tracking_read

import sftp_avl2db
import sftp_avl2db.config as cfg
import crm_dbspec

bad = [p for p in opened if ".env" in p]
if bad:
    raise AssertionError("import 期间读了 .env: " + repr(bad))

# 模块级没有执行 load_values()：显式调用才会在缺 .env 且无 SFTP_* 时抛错
try:
    cfg.load_values()
except FileNotFoundError:
    pass
else:
    raise AssertionError("load_values() 应在缺 .env 时抛 FileNotFoundError")
print("IMPORT_CLEAN")
'''
        import json
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, "-c", code, str(ROOT), json.dumps(_clean_env())],
                cwd=tmp, capture_output=True, text=True,
            )
        self.assertEqual(r.returncode, 0, f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}")
        self.assertIn("IMPORT_CLEAN", r.stdout)

    # ---- 2. 与冻结快照的逐字段对照 ----
    def test_load_profiles_matches_legacy(self):
        for name, p in self.profiles.items():
            with self.subTest(task=name):
                legacy = LEGACY_SPECS["tasks"][name]
                self.assertEqual(list(p.file_prefixes), legacy["FILE_PREFIXES"])
                for key in ("host", "port", "username", "remote_path"):
                    self.assertEqual(getattr(p.sftp, key), legacy["SFTP"][key], key)
                # 快照刻意不含密码：只断言它确实来自 .env，不比对明文
                self.assertTrue(p.sftp.password)
                self.assertEqual(p.sftp.password,
                                 load_values(ROOT / ".env")["SFTP_PASSWORD"])
                self.assertEqual(p.retention.batch_size, legacy["BATCH_SIZE"])
                self.assertEqual(p.retention.data_days, legacy["DATA_EXPIRE_DAYS"])
                self.assertEqual(p.retention.local_file_days,
                                 legacy["LOCAL_FILE_EXPIRE_DAYS"])
                self.assertEqual(p.retention.poll_seconds,
                                 legacy["POLL_INTERVAL_SECONDS"])
                self.assertEqual([list(c) for c in p.schema],
                                 [list(c) for c in legacy["DB_SCHEMA"]])
                self.assertEqual(p.num_tables, legacy["NUM_TABLES"])
                self.assertEqual(p.table_name_prefix,
                                 legacy["TABLE_NAME_PREFIX"])
                self.assertEqual(p.db_dir.resolve(),
                                 (ROOT / legacy["db_dir_suffix"]).resolve())
                self.assertEqual(p.columns, crm_dbspec.db_columns(name))
                # 查询声明也锁进快照，防止静态契约被改动
                self.assertEqual(crm_dbspec.QUERY_SPECS[name], legacy["QUERY"])

    def test_default_paths_follow_legacy(self):
        base = ROOT / "tasks"
        for name, p in self.profiles.items():
            with self.subTest(task=name):
                self.assertEqual(p.log_dir.resolve(), (base / "log").resolve())
                self.assertEqual(p.state_dir.resolve(), (base / "state").resolve())
                self.assertEqual(p.local_dir.resolve(), (base / "files" / name).resolve())
                self.assertEqual(p.db_dir.resolve(), (base / "sqlite_db" / name).resolve())
                self.assertEqual(p.fetched_path.name, f"{name}.fetched")
                self.assertEqual(p.lock_path.name, f"{name}.lock")

        # 远程文件名沿用现有 {前缀}{YYYYMMDD}000000.AVL 约定。
        # 这里直接断言字面量，避免测试为了取旧模块常量而 import paramiko
        # （base_task.py 顶部 import 了它），让本套件只依赖标准库。
        for name, p in self.profiles.items():
            expected = [f"{pre}20261110000000.AVL" for pre in p.file_prefixes]
            self.assertEqual(list(iter_remote_names(p, "20261110")), expected)

    # ---- 3. dotenv 解析与冻结期望逐字一致 ----
    def test_dotenv_parser_verbatim(self):
        case = LEGACY_SPECS["dotenv_case"]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "case.env"
            p.write_text(case["content"], encoding="utf-8")
            with mock.patch.dict(os.environ, _clean_env(), clear=True):
                self.assertEqual(parse_dotenv(p), case["expected"])

    # ---- 4. 环境变量优先级 ----
    def test_env_priority_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "custom.env"
            env_file.write_text(
                "SFTP_HOST=from_file\nSFTP_PASSWORD=pwd\nSFTP_PORT=22\n",
                encoding="utf-8")

            with mock.patch.dict(os.environ, _clean_env(), clear=True):
                os.environ["SFTP_HOST"] = "from_system"
                self.assertEqual(load_values(env_path=env_file)["SFTP_HOST"],
                                 "from_system")

            with mock.patch.dict(os.environ, _clean_env(), clear=True):
                os.environ["TASKS_ENV_FILE"] = str(env_file)
                v = load_values()
                self.assertEqual(v["SFTP_HOST"], "from_file")
                self.assertEqual(v["SFTP_PASSWORD"], "pwd")

            # 缺 .env 且环境变量不足 → 显式调用才报错，import 不受影响
            with mock.patch.dict(os.environ, _clean_env(), clear=True):
                with self.assertRaises(FileNotFoundError):
                    load_values(env_path=Path(tmp) / "missing.env")

    # ---- 5. validate_specs 复现原有校验 ----
    def test_validate_specs_accepts_current_profiles(self):
        self.assertIsNone(crm_dbspec.validate_specs(self.profiles))

    def test_validate_specs_rejects_key_drift(self):
        with self.assertRaises(ValueError):
            crm_dbspec.validate_specs(
                {"opportunity_recall": self.profiles["opportunity_recall"]})

    def test_validate_specs_rejects_unknown_where_column(self):
        p = self.profiles["opportunity_recall"]
        broken = replace(p, schema=tuple(c for c in p.schema
                                         if c[0] != "product_no"))
        with self.assertRaisesRegex(ValueError, "不在 DB_SCHEMA"):
            crm_dbspec.validate_specs({"opportunity_recall": broken,
                                       "loss_control": self.profiles["loss_control"]})

    def test_validate_specs_rejects_bad_mode(self):
        specs = copy.deepcopy(crm_dbspec.QUERY_SPECS)
        specs["loss_control"]["mode"] = "multi_db_multi_table"
        with mock.patch.object(crm_dbspec, "QUERY_SPECS", specs):
            with self.assertRaisesRegex(ValueError, "不支持的 QUERY.mode"):
                crm_dbspec.validate_specs(self.profiles)

    def test_validate_specs_rejects_db_map_outside_prefixes(self):
        specs = copy.deepcopy(crm_dbspec.QUERY_SPECS)
        specs["opportunity_recall"]["db_map"]["x"] = "F09999"
        with mock.patch.object(crm_dbspec, "QUERY_SPECS", specs):
            with self.assertRaisesRegex(ValueError, "不在 FILE_PREFIXES"):
                crm_dbspec.validate_specs(self.profiles)

    def test_validate_specs_rejects_missing_shard_config(self):
        p = replace(self.profiles["loss_control"], num_tables=0,
                    table_name_prefix="")
        with self.assertRaisesRegex(ValueError, "NUM_TABLES / TABLE_NAME_PREFIX"):
            crm_dbspec.validate_specs({"opportunity_recall": self.profiles["opportunity_recall"],
                                       "loss_control": p})

    def test_validate_specs_rejects_missing_fixed_table_name(self):
        specs = copy.deepcopy(crm_dbspec.QUERY_SPECS)
        specs["loss_control"]["mode"] = "single_db_single_table"
        with mock.patch.object(crm_dbspec, "QUERY_SPECS", specs):
            with self.assertRaisesRegex(ValueError, "table_name"):
                crm_dbspec.validate_specs(self.profiles)

    # ---- 6. 其他项目不带 .env 也能用 ----
    def test_from_mapping_without_any_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            d = {
                "name": "my_task",
                "file_prefixes": ["F0XXXX"],
                "sftp": {"host": "h", "port": 22, "username": "u",
                         "password": "p", "remote_path": "/r"},
                "runtime_dir": str(runtime),
                "schema": [["op_time", "TEXT"], ["product_no", "TEXT NOT NULL"]],
                "parser": "flat",
                "routing": "per_prefix",
                "batch_size": 100,
            }
            with mock.patch.dict(os.environ, _clean_env(), clear=True):
                os.environ["TASKS_ENV_FILE"] = "/nonexistent/no-such.env"
                p = TaskProfileConfig.from_mapping(d)
            self.assertEqual(p.name, "my_task")
            self.assertEqual(p.file_prefixes, ("F0XXXX",))
            self.assertEqual(p.sftp.host, "h")
            self.assertEqual(p.retention.batch_size, 100)
            self.assertEqual(p.columns, ["op_time", "product_no"])
            self.assertEqual(p.local_dir, (runtime / "files" / "my_task").resolve())
            self.assertEqual(p.db_dir, (runtime / "sqlite_db" / "my_task").resolve())
            self.assertEqual(p.log_dir, (runtime / "log").resolve())
            self.assertEqual(list(iter_remote_names(p, "20261110")),
                             ["F0XXXX20261110000000.AVL"])
            # 默认字段也完整
            self.assertEqual(p.num_tables, 0)
            self.assertEqual(p.remote_name_tmpl, "{prefix}{date}000000.AVL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
