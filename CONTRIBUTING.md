# 贡献指南

感谢参与invoice-master。这个项目还年轻，任何形式的反馈都很宝贵。

## 报告 Bug

请附上：

1. 操作系统与 Python 版本
2. 复现命令（`invoice-master extract ./发票/ -v`）
3. 发票样本（**请先脱敏**——去掉公司名、税号、金额等敏感信息）
4. 实际输出 vs 期望输出

> ⚠️ 不要提交含真实公司名称、税号、账号的发票样本。发票号码和金额也应替换为测试值。

## 开发环境

```bash
git clone https://cnb.cool/52hz.ltd/Skills/invoice-master
cd invoice-master
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## 提交前必做

```bash
pytest                          # 测试必须全绿
ruff check .                    # 代码检查
ruff format .                   # 格式化
```

## 代码风格

- **注释写「为什么」，不写「做了什么」**。参考 `models.py` 里 `parse_amount`
  对会计负数括号和 `%`号的说明——这些是踩坑记录，不是 API 文档。
- **金额绝不用 `float`**，统一 `Decimal`。这是硬性要求，见 `models.py` 顶部。
- **破坏性操作默认 dry-run**。新增 CLI 命令时，如果会改动用户文件，
  必须默认只预览。
- **不要静默吞异常**。`merger.py` 里 `_copy_stamps` 的注释记录了一个教训：
  `except Exception: logger.debug(...)` 掩盖了「`fitz` 未导入」这个 bug 很久。
  该警告就 `logger.warning`。

## 添加票种

```python
from invoice_master import BaseExtractor, DocType, register
from invoice_master.extractor import FieldRule


@register
class GasTicketExtractor(BaseExtractor):
    keywords = ("加油票", "成品油")  # 必须能在票面顶部 120 字符内命中
    type_name = DocType.FUEL
    priority = 45

    amount_rules = (FieldRule("金额", (r"价税合计[^\d]*([\d.]+)",)),)
```

并补充对应测试 —— 特别是"公司名不被截断"、"号码不被当金额"这两类，
它们是历史上最常见的 bug。

## 提交信息

```
类型(范围): 简要说明

类型：feat / fix / docs / test / refactor / perf / chore
范围：模块名，如 extractor / merger / mailbox / cli
```

示例：

```
fix(extractor): 公司名不再被企业后缀截断

惰性量词下"某某科技有限公司"会在"科技"处提前截断，丢掉"有限公司"，
导致买卖方匹配失败。改为逐行贪婪匹配 + 长后缀分支。
```

## 需要帮忙的地方

- **票种覆盖**：定额发票、加油票、停车费、出租车电子发票等
- **版面样本**：不同平台（滴滴/华住/国航/12306）导出的 PDF 版面差异很大，
  有真实样本最好
- **准确率评测**：拿一批真实发票跑一遍，统计字段正确率

## 不接受的贡献

- 引入重框架（pydantic、pandas 等）作为**必需**依赖——保持零重依赖是核心设计
- 把「LLM 兜底」改成默认开启——隐私优先是本项目的卖点
- 上传真实发票样本到仓库