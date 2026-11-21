"""测试路径设置。

让测试直接针对工作树中的 ``scripts/invoice_master`` 运行，
无需先 ``pip install -e .`` 即可执行 ``pytest tests/ -q``。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
