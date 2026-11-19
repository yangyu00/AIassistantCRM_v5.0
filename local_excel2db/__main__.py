# -*- coding: utf-8 -*-
"""``python -m local_excel2db`` 的进程退出点；逻辑与退出码在 ``cli.main``。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
