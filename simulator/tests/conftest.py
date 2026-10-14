"""Make `import robot_model` work no matter where pytest is started from.

simulator/ 不是一个 Python 包（没有 __init__.py，容器里直接 `python sim.py` 运行），
所以测试运行前把 simulator/ 目录放进 sys.path。CI 里是从仓库根目录跑 `pytest simulator/tests`。
"""

import sys
from pathlib import Path

SIMULATOR_DIR = Path(__file__).resolve().parents[1]
if str(SIMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_DIR))
