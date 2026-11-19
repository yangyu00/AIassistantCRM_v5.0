# -*- coding: utf-8 -*-
"""``python -m sftp_avl2db`` 入口；参数与退出码见 ``cli.main()``。

    python -m sftp_avl2db --task opportunity_recall
    python -m sftp_avl2db --task loss_control --date 20261110
    python -m sftp_avl2db --task loss_control --date 20261110 --force-reload
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
