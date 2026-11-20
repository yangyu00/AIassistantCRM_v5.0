# -*- coding: utf-8 -*-
"""``python -m call_stat`` 的进程退出点；参数与退出码在 ``cli.main``。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
