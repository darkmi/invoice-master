---
name: invoice-master
slug: invoice-master
displayName: 电子发票大师
version: 0.1.0
summary: 从邮箱收集电子发票，识别字段、归档、合并打印并生成报销汇总表；扫描件可由 agent 直接视觉识别，也支持可选远程 LLM 兜底。
description: 电子发票整理与报销准备。当用户需要处理发票时使用 —— 从邮箱收集电子发票、识别发票字段（金额/日期/购销方/号码）、按票据类型归档、批量重命名、合并成报销单打印、生成 Excel 报销汇总表、检查发票与凭证配对。触发词：发票、报销、报销单、合并发票、整理发票、发票汇总、发票归档、重命名发票、邮箱收发票、invoice、报销汇总表。
license: Apache-2.0
tags: [发票, 报销, PDF, IMAP, 效率工具]
homepage: https://cnb.cool/52hz.ltd/Skills/invoice-master
agent_created: true
---

# invoice-master

## 概述

把散落在邮箱和文件夹里的发票，变成可提交的报销材料。

**这个 skill 是代码封装，不是指令模板。** 核心逻辑（PDF 解析、正则提取、
Decimal 记账、配对算法）已经固化成 Python 脚本，不要现场手写正则或重新实现——
那样每次结果都不一样，且无法测试。正确做法是调用 CLI。

## 何时使用

使用本skill 当用户需要：

- **收集发票** —— "帮我把这三个月的发票下载下来" / "从邮箱里把发票都捞出来"
- **识别发票** —— "这些 PDF 都是多少钱" / "提取发票的金额和日期"
- **整理归档** —— "按类型分文件夹" / "把发票改名成规范格式"
- **合并打印** —— "把这些发票合并成报销单，每页放4 张"
- **生成报表** —— "做一个报销汇总表" / "统计这个月发票总额"
- **检查完整性** —— "看看有没有发票缺行程单"

不要用于：

- 记账 / 财务核算 / 税务申报 —— 本工具只做文件处理，不做账务判断
- 发票真伪查验 —— 无此能力，不要承诺
- 代替人工审核 —— 报销前用户必须自行核验

## 核心原则（必须遵守）

### 1. 正则优先；需要视觉识别时优先用 agent 自身能力

默认引擎是 `local`（本地正则，`llm.enabled: false`），不产生 API 调用。

本 skill 是给 agent 用的。扫描件或复杂版面需要视觉识别时，**优先走 agent 原生路线**：
`agent-prepare` 生成任务包 → agent 用自己的多模态能力查看图片并回填答案 → `agent-apply`。
这条路线不需要配置 GLM/OpenAI API Key，也不会让 CLI 再发一次远程请求。

`--engine hybrid` / `dual` 是**备选**远程路线，会把发票图片发送至第三方模型服务。
只有 agent 没有视觉能力、或用户明确指定时才使用，且必须先告知：
> 启用远程 AI 引擎意味着发票图片会发送至第三方模型服务（请确认服务商）。
> 费用约每张 1000-3000 tokens。

注意：agent 自身也是云端模型；「用 agent 能力」不等于发票不出本机，
只是不再额外配置一套远程 GLM API。

### 2. 破坏性操作先预览

`rename` 和任何移动/删除操作**默认只预览**。必须先跑一次给用户看结果，
得到确认后再加 `--execute`。

绝对不要主动删除用户的发票文件。

### 3. 金额绝不手工计算

所有金额统计交给 `report` 命令（内部用 `Decimal`）。
心算或用 float 累加会产生静默的对账错误。

### 4. 凭据不落地

邮箱授权码、API Key 通过环境变量传入，不写进配置文件、不打印到日志。

## 环境准备

首次使用先确认依赖：

```bash
# 检查 Python 和依赖
python --version  # 需 3.9+
python -c "import fitz" || pip install pymupdf openpyxl pyyaml
```

如果项目尚未安装：

```bash
cd <项目目录>
pip install -e .
```

## 工作流

### 场景 A：从邮箱收集并整理

```bash
# 1. 先确认配置（凭据用环境变量，不落盘）
export INVOICE_MASTER_MAILBOX_PASSWORD="<授权码>"
# 编辑 invoice_master.yaml 填 host / username

# 2. 试跑，只统计不下载
invoice-master scan -o ./新发票 --dry-run

# 3. 确认无误后正式收集
invoice-master scan -o ./新发票 --days 180

# 4. 提取字段，看识别效果
invoice-master extract ./新发票
```

### 场景 B：整理已有发票文件

```bash
# 1. 提取字段（先看哪些识别不了）
invoice-master extract ./发票/

# 2. 检查配对完整性（打车发票是否缺行程单）
invoice-master pair ./发票/

# 3. 预览重命名（注意：默认只预览）
invoice-master rename ./发票/ -t "{date}-{type}-{amount}元"

# 4. 用户确认后执行
invoice-master rename ./发票/ -t "{date}-{type}-{amount}元" --execute

# 5. 生成汇总表
invoice-master report ./发票/ -o 3月报销.xlsx
```

### 场景 C：合并成报销单

```bash
# 每页 4 张，A4 横向
invoice-master merge ./发票/ -o 报销单.pdf --layout 横向2x2

# 细长票据（如火车票）用竖向
invoice-master merge ./火车票/ -o 报销单.pdf --layout 竖向1x2

# 自定义布局：1x1–10x10 任意行列（吸收自 piaoyihe）
invoice-master merge ./发票/ -o 报销单.pdf --layout 横向3x5

# 排版细节：页边距(mm)、间隙(pt)、裁切线、分批大小
invoice-master merge ./发票/ -o 报销单.pdf -l 竖向10x10 \
    --margin-top 12 --gap 5 --crop-marks --batch-size 20

# 查看所有可用布局
invoice-master layouts
```

常用布局：`横向2x2`（每页 4 张）/`横向2x4`/`横向3x3`/`竖向1x2`（细长票 2 张）/
`竖向1x3`/`竖向2x4`；自定义 `横向RxC`/`竖向RxC`（R=行、C=列，各 1–10）。
⚠️ 竖向预设名数字是「列×行」，与动态写法「行×列」相反：`竖向1x2` 实际是 2 行 1 列；
要竖向纸 1 行 2 列请写 `portrait1x2` 或 `--rows 1 --cols 2`。

排版参数：`--page A4|A3|LETTER`（默认 A4）、`--margin-*`（mm，默认 10）、
`--gap`（pt，默认 5）、`--crop-marks`、`--no-dividers`、`--batch-size`（默认 50，
大批量防内存耗尽）、`--mode vector|raster`（默认 vector，兼容叫法 普通/图像）。
日常用 `vector`；矢量模式下图层/注释丢失或错位改 `raster`。监制章在 vector 模式下
会自动以 450 DPI 贴回。优先级：命令行单项参数 > `--layout` > 配置文件
`merge:` 段 > 内置默认。

合并后可导出为图片给用户预览效果，确认版式无误。

### 场景 D：识别效果差，优先用 agent 自己的视觉能力

```bash
# 1. 先看哪些文件没识别出来
invoice-master extract ./发票/ --json

# 2. 生成 agent 识别任务包（PDF 会渲染成 PNG，附本地提示与统一 prompt）
invoice-master agent-prepare ./发票/ -o agent_request.json --images-dir ./agent_images
```

接着由**你（agent）**执行，不要调用 GLM API：

1. 读取 `agent_request.json`，查看 `instructions`、`prompt` 和 `answer_format`。
2. 对 `tasks` 中每个有 `image` 的任务，用你自己的图片查看能力读取该图片。
3. 结合 `hint`（本地正则已识别的字段）逐张抽取字段。
4. 写出 `agent_answers.json`，格式：

```json
{
  "answers": [
    {"id": "<task.id>", "fields": {"doc_type": "增值税专票", "amount": "1060.00"}}
  ]
}
```

然后回填并复用结果：

```bash
# 3. 转为与 extract 相同的标准输出
invoice-master agent-apply agent_request.json agent_answers.json -o extracted.json

# 4. 直接基于该结果生成报表，不重复识别
invoice-master report --from-extract extracted.json -o 3月报销.xlsx
```

`agent-apply` 会复用与远程 LLM 相同的字段解析与交叉校验：
本地正则已经识别出的字段优先，agent 只补空缺。

四套 CLI 提取引擎仍可切换：

```bash
#   local    本地正则（默认，无 API 调用）
#   hybrid   本地 + 远程 LLM 兜底
#   dual     InvoiceFlowAI 双引擎 OCR+LLM → Vision → 本地兜底
#   piaoyihe 票易合 7 票种工厂提取器
invoice-master extract ./发票/ --engine dual
invoice-master extract ./发票/ --engine piaoyihe
```

`dual` 需要远程模型服务配置；缺依赖或配置时会自动回退到 `local` 并提示，不会静默失败。
**能在 agent 侧看图时，不必走这条远程路线。**

安全校验（吸收的独有能力）：

```bash
invoice-master audit --truth-manifest truth.json --run-root ./run   # 真值审计
invoice-master urlcheck https://example.com/invoice.pdf             # SSRF 防护
invoice-master verify ./归档/ --truth-manifest truth.json           # 批次准入
```

## 常见处理

### 扫描结果为 0 —— 先查日期范围，别急着下结论

**这是实测踩过的坑**：某 QQ 邮箱 IMAP 最早只能拉到 9/16，
但用户在网页端能看到 9/2 的邮件。IMAP 能看到的历史范围
不一定等于网页端。

```bash
# 查实际能拉到多少、覆盖什么时间段
invoice-master scan -o /tmp/probe --days 365 -v 2>&1 | grep -E "扫描|命中"
```

若结果为 0，按顺序排查：

1. **IMAP 历史范围不够** —— 告知用户这个事实并给出证据
   （实际日期范围、邮件总数）。解决办法：邮箱设置里开启
   「IMAP 收取全部邮件」，或让用户手动下载附件。
2. **邮件在别的文件夹** —— 部分服务的发票邮件不进 INBOX。
   可用 `invoice-master scan --mailbox-name "其他文件夹"` 指定。
3. **确实是扫描件/图片版发票** —— 用 `agent-prepare` / `agent-apply`
   走 agent 自身视觉识别；只有无法看图时才改用 `--enable-llm`。

**汇报时必须给证据**（实际日期范围 + 邮件总数），不要只说
"没找到发票邮件"—— 用户会以为工具坏了。

### 大量文件报"需复核"

先看是不是扫描件（无文本层）：

```bash
invoice-master extract ./发票/ -v 2>&1 | grep no_text_layer
```

扫描件需要视觉识别，正则无能为力——优先用 agent 自身的视觉能力走
`agent-prepare` / `agent-apply`；这点要如实告知用户，
不要让用户以为换个参数就能解决。

### 票种识别错误

比如增值税专票被判成了餐饮票。原因是商品明细里出现了
"餐费""酒店"等词。查看实际判断结果：

```bash
invoice-master extract ./发票/ --json | python -m json.tool
```

如果确认是规则问题，收集该票样本后建议用户提issue 或按
CONTRIBUTING.md 新增票种提取器。

### 重命名后的文件名有乱码

模板里不要用未识别的占位符。合法占位符：

| 占位符 | 含义 |
|--------|------|
| `{date}` / `{date_compact}` | 开票日期 |
| `{type}` | 票据类型 |
| `{amount}` | 金额 |
| `{seller}` / `{purchaser}` | 销售方 / 购买方 |
| `{code}` | 发票号码后 8 位 |
| `{index}` / `{index:03d}` | 序号 |

### 合并后发票上的章不见了

增值税发票的监制章是 PDF 注释对象。矢量合并（`vector` / `普通`）不直接复制注释，
但会自动把印章区域以 450 DPI 渲染后贴回原位——看起来"变成图片"是正常的。
若确实缺失或位置不对，改用 `--mode raster`（图像）整页转图重试。

## 汇报方式

处理完成后，向用户汇报要包含：

1. **实际处理数量** —— 不是估计值，是脚本返回的计数
2. **未能识别的文件清单** —— 逐个列出文件名和原因
3. **缺失的凭证** —— 若做了配对检查
4. **报表/文件位置** —— 完整路径

示例：

> 已处理 47 张发票，其中 45 张成功识别。
>
> 2 张需人工复核：
> - `IMG_2038.jpg` — 扫描件，无文本层
> - `发票扫描_001.pdf` — 版式未识别
>
> 发现 3 张打车发票缺少对应行程单。
>
> 汇总表已生成：`D:\报销\3月报销.xlsx`
> 合计 ¥12,847.30（含税额 ¥986.15）

**不要**说"已全部处理完成"这类模糊表述——用户需要知道哪里出了问题。

## 排障

```bash
invoice-master extract ./发票/ -v    # 调试日志
invoice-master config --show          # 查看当前配置（密码自动脱敏）
invoice-master config --init x.yaml  # 生成默认配置模板
```

常见错误：

| 现象 | 原因 | 处理 |
|------|------|------|
| `ModuleNotFoundError: fitz` | 缺 pymupdf | `pip install pymupdf` |
| 邮箱认证失败 | 用了登录密码而非授权码 | QQ/163 需在设置里开启 IMAP 获取授权码 |
| 扫描数量很少 | 邮箱默认只保留近 30 天 | 调大 `--days`，或在邮箱设置里选"收取全部邮件" |
| 找不到配置文件 | 未指定 | `invoice-master config --init invoice_master.yaml` |

## 参考

- 项目文档：README.md
- 贡献指南：CONTRIBUTING.md（新增票种的方法）
- 配置示例：invoice_master.example.yaml