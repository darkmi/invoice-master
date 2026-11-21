"""invoice-master —— 电子发票整理与报销准备工具。

从邮箱收集电子发票，识别字段、按类型归档、合并打印、生成报销汇总表。

设计原则：

* **正则优先**：字段抽取默认走本地正则，扫描件/复杂版面可启用兜底
* **Agent 原生视觉**：可由 agent 直接看图回填字段，无需另配 GLM/OpenAI API
* **LLM 可选兜底**：远程 LLM 默认关闭，按需启用，且带熔断保护
* **Decimal 记账**：金额绝不用 float
* **只读邮箱**：IMAP 全程``readonly``，不改动任何邮件
* **默认 dry-run**：批量改名/归档必须显式确认才执行

快速上手::

    from invoice_master import load_config, HybridExtractor, Reporter

    config = load_config("invoice_master.yaml")
    extractor = HybridExtractor(config)
    results = extractor.extract_many(["发票1.pdf", "发票2.pdf"])

    invoices = [r.invoice for r in results if r.invoice]
    Reporter(config).build(invoices, "汇总.xlsx")

整合模块::

    invoice_master.invoiceflow  InvoiceFlowAI 非 GUI 内核（AI 识别 / 真值审计 / 链接恢复）
    invoice_master.piaoyihe     票易合内核（PDF 排版 / 多票种提取 / FastMCP Server）

两个整合模块保留各自原始的导入风格（扁平导入 / 顶层包导入），
本包统一把所需目录加入 sys.path，使这些导入无需改写即可工作。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# 统一注入三条模块搜索路径：
# 1) 本包目录 —— 常规 from invoice_master.xxx import
# 2) invoiceflow 目录 —— 原始扁平导入 from pairing_engine import ...
# 3) piaoyihe 目录 —— 原始顶层包导入 from core.xxx / from mcp_server.xxx
_PKG_DIR = _Path(__file__).resolve().parent
for _extra in (_PKG_DIR, _PKG_DIR / "invoiceflow", _PKG_DIR / "piaoyihe"):
    if str(_extra) not in _sys.path:
        _sys.path.insert(0, str(_extra))

__version__ = "0.1.0"
__author__ = "invoice-master contributors"
__license__ = "Apache-2.0"

from .agent_bridge import (
    apply_agent_request,
    build_agent_request,
    index_answers,
    invoice_from_row,
    write_agent_request,
)
from .config import DEFAULT_CONFIG, Config, load_config, redact, save_config
from .extractor import BaseExtractor, Extractor, ExtractResult, register
from .llm import CircuitBreaker, HybridExtractor, LLMExtractor
from .mailbox import (
    INVOICE_EXTENSIONS,
    FetchedAttachment,
    ImapFetcher,
    MailboxError,
    MailFilter,
    ScanStats,
    is_invoice_attachment,
    iter_archive,
)
from .merger import PRESET_LAYOUTS, MergeError, MergeLayout, Merger, resolve_layout
from .models import (
    COMPANION_ONLY,
    COMPANION_REQUIRED,
    DocType,
    Invoice,
    Pair,
    PairingResult,
    Route,
    companies_match,
    format_amount,
    format_date,
    pair_invoices,
    parse_amount,
    parse_date,
)
from .reporter import (
    ArchiveResult,
    RenamePlan,
    Reporter,
    archive_by_type,
    build_filename,
    execute_rename,
    plan_rename,
    sanitize_filename,
)
from .rules import ALL_EXTRACTORS, get_extractor

__all__ = [
    "__version__",
    # 配置
    "Config",
    "DEFAULT_CONFIG",
    "load_config",
    "save_config",
    "redact",
    # 模型
    "DocType",
    "Invoice",
    "Route",
    "Pair",
    "PairingResult",
    "COMPANION_ONLY",
    "COMPANION_REQUIRED",
    "pair_invoices",
    "companies_match",
    "parse_amount",
    "parse_date",
    "format_amount",
    "format_date",
    # 提取
    "Extractor",
    "ExtractResult",
    "BaseExtractor",
    "register",
    "ALL_EXTRACTORS",
    "get_extractor",
    # Agent 原生视觉识别桥接
    "build_agent_request",
    "write_agent_request",
    "index_answers",
    "apply_agent_request",
    "invoice_from_row",
    # LLM
    "HybridExtractor",
    "LLMExtractor",
    "CircuitBreaker",
    # 合并
    "Merger",
    "MergeLayout",
    "MergeError",
    "PRESET_LAYOUTS",
    "resolve_layout",
    # 报表与归档
    "Reporter",
    "RenamePlan",
    "ArchiveResult",
    "plan_rename",
    "execute_rename",
    "build_filename",
    "sanitize_filename",
    "archive_by_type",
    # 邮箱
    "ImapFetcher",
    "MailFilter",
    "MailboxError",
    "FetchedAttachment",
    "ScanStats",
    "iter_archive",
    "INVOICE_EXTENSIONS",
    "is_invoice_attachment",
]
