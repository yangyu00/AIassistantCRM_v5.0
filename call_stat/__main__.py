"""``python -m call_stat`` 的进程退出点。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
