"""票易合（piaoyihe）非 GUI 功能包（已整合进 invoice_master）。

原始项目把 ``core/`` 与 ``mcp_server/`` 作为两个顶层包，模块间用
``from core.xxx import`` / ``from mcp_server.xxx import`` 绝对导入。
路径注入已统一上移到 ``invoice_master/__init__.py``，由外层入口完成。

已剥离 PySide6 GUI（code/ui/、main.py、core/update_checker.py），
仅保留纯业务内核与 FastMCP Server。

Apache License 2.0 — 票易合项目
See LICENSE in this directory.
"""

__all__ = []
