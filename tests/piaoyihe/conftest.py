"""票易合测试的共享路径配置。

原项目测试用 ``from core.xxx`` / ``from mcp_server.xxx`` 绝对导入，
依赖 ``code/`` 在 sys.path 中。整合后两个顶层包位于
scripts/invoice_master/piaoyihe/，这里注入该目录使原始测试代码无需改写。
"""

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[2] / "scripts" / "invoice_master" / "piaoyihe"
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
