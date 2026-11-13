# -*- coding: utf-8 -*-
"""定时任务包：把原 main.py startup 里托管的 3 个 SFTP 拉取/入库任务拆成独立进程。

    tasks/task_f09554_opp.py   任务1 回捞商机（F09554/F09555/F09556，12列，无续行）
    tasks/task_f09591_loss.py  任务2 控损    （F09591，5逻辑列 + 续行拼接 + 128分表）
    tasks/task_f09609_diag.py  任务3 诊断    （F09609，宽表两两展开 + 续行 + 128分表）

每个脚本可直接 `python tasks/task_xxxx.py` 独立运行，互不干扰，各自：
  - 独立轮询循环与间隔（POLL_INTERVAL_SECONDS）
  - 独立日志（tasks/log/{任务名}_INFO/ERROR_日期.log）
  - 独立「已入库清单」（tasks/state/{任务名}.fetched）实现断点续传、不重复拉取
  - 独立单实例文件锁（tasks/state/{任务名}.lock），与仍存活的 main.py startup 线程互斥
  - 独立本地文件目录（tasks/files/{任务名}/）与 sqlite 库（tasks/sqlite_db/{任务名}/）

库表结构与分表路由键刻意保持与原 sftp_workdir_xxxxx/sftp_service.py 完全一致，
因此在线查询（data_query.py）无需任何改动即可继续读到数据。
"""
