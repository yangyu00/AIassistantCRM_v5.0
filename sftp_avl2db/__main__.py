# -*- coding: utf-8 -*-
"""``python -m sftp_avl2db`` 入口。

    python -m sftp_avl2db --task opportunity_recall
    python -m sftp_avl2db --task loss_control --date 20261110
    python -m sftp_avl2db --task loss_control --date 20261110 --force-reload

旧的 ``python -m tasks.task_opportunity_recall`` /
``python -m tasks.task_loss_control`` 在阶段 5 删除 ``tasks/*.py`` 后不再可用。
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
