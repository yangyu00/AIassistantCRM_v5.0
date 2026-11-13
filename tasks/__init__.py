# -*- coding: utf-8 -*-
"""定时任务包：3 个 SFTP 拉取/入库任务各自作为独立进程运行。

    python -m tasks.task_f09554_opp    任务1 回捞商机（F09554/55/56，12 列，无续行）
    python -m tasks.task_f09591_loss   任务2 控损（F09591，5 逻辑列 + 续行 + 128 分表）
    python -m tasks.task_f09609_diag   任务3 诊断（F09609，宽表两两展开 + 128 分表）

每个任务有独立的轮询、日志（tasks/log/）、入库清单（tasks/state/*.fetched）、
单实例锁（tasks/state/*.lock）与 sqlite 库目录（tasks/sqlite_db/{任务名}/）。

配置由 tasks/config.py 解析根目录 .env；在线查询 data_query.py 复用同一份配置
与 db_dir() 指向的库目录。在线业务 main.py 不参与数据更新。
"""
