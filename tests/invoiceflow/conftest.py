import sys
from pathlib import Path

import pytest

# InvoiceFlowAI 使用扁平模块导入，把模块目录加入 sys.path
# 整合后模块位于 scripts/invoice_master/invoiceflow
_MODULES_DIR = Path(__file__).resolve().parents[2] / "scripts" / "invoice_master" / "invoiceflow"
if str(_MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULES_DIR))


@pytest.fixture(autouse=True)
def isolate_windows_user_data(monkeypatch, tmp_path: Path):
    """Keep tests from reading or overwriting the operator's real app data."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

