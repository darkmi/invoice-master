# 电子发票大师（invoice-master）

**给 AI Agent 用的发票整理 Skill。** 对 agent 说一句话，完成：收集发票 → 识别字段 → 归档 → 合并打印 → 生成报销汇总表。

这是一个 skill 项目：`SKILL.md` 是 agent 的执行指令，`scripts/invoice_master` 是背后的 CLI 工具包。
装进 agent 的技能目录后，直接用自然语言吩咐即可，agent 会按 `SKILL.md` 的工作流自动调用命令、
在改动文件前先预览、并把金额统计交给 Decimal 计算。

---

## ✨ 能做什么

| 能力 | 说明 |
|------|------|
| 📧 **邮箱收集** | IMAP 只读抓取，四层漏斗精准过滤，不改动任何邮件 |
| 🔍 **字段识别** | 10 类票据的本地正则提取，无需网络 |
| 👁️ **Agent 视觉识别** | 扫描件/复杂版面由 agent 直接看图回填字段，无需模型 API |
| 📁 **智能归档** | 按票据类型自动分文件夹，非目标公司隔离 |
| 🖨️ **合并打印** | 6 种预设 + 1x1–10x10 自定义布局，A4/A3/Letter 横竖排，监制章自动贴回，支持图片混排 |
| 📊 **Excel 汇总** | 分类小计 + 总计，重复发票号码自动标黄 |
| 🔗 **凭证配对** | 打车发票↔行程单、住宿发票↔水单，自动匹配 |
| 🔒 **凭据安全** | 邮箱凭据只从环境变量读取，不写入仓库 |

---

## 📦 安装

### 1. 安装为 Skill

- **WorkBuddy / SkillHub**：安装「电子发票大师」（slug `invoice-master`）；
  或把本仓库目录放入技能目录（全局 `~/.workbuddy/skills/`，或项目 `.workbuddy/skills/`）
- **其他 agent 平台**：把仓库目录放入其技能目录，确保 `SKILL.md` 位于顶层即可

安装后，agent 读取 `SKILL.md` 就知道何时调用、按什么顺序处理。

### 2. 安装依赖

skill 的执行体是命令行工具，首次使用先装依赖（**Python 3.9+**）：

```bash
cd <skill目录>

# 建议用虚拟环境
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -e .
```

核心依赖只有 `pymupdf`、`openpyxl`、`pyyaml` 三个。

---

## 🗣️ 怎么用：吩咐 agent 即可

| 你说 | agent 调用 |
|------|-----------|
| 「把邮箱里这 3 个月的发票都捞出来」 | `scan` |
| 「这些 PDF 都是多少钱、什么日期」 | `extract`（扫描件走视觉识别） |
| 「按类型分文件夹、文件名改规范」 | `rename`（默认先预览） |
| 「合并成报销单，每页放 4 张」 | `merge --layout 横向2x2` |
| 「做一张 3 月报销汇总表」 | `report` |
| 「看看打车发票是不是都配了行程单」 | `pair` |
| 「识别效果不好，帮我兜底」 | 视觉识别 / `--engine` 切换 |

### 典型一条龙

```
scan（收集） → extract（识别） → pair（查配对） → rename（预览→确认→执行）
→ merge（合并报销单） → report（Excel 汇总）
```

两条 agent 遵守的硬性规则（`SKILL.md` 里有完整约束）：

1. **破坏性操作先预览** —— `rename` 等默认只预览，用户确认后才 `--execute`
2. **金额绝不手工计算** —— 统计一律交给 `report`（内部用 Decimal）

---

## 👁️ 识别方式

### 本地正则（默认）

文本型 PDF 直接正则提取，快、稳、无外部依赖。增值税发票等标准化版面下，
正则比视觉模型更可靠。

### Agent 视觉识别（扫描件 / 复杂版面）

CLI 是 agent 的子进程，拿不到 agent 自身的模型，所以视觉识别做成两段式握手——
**看图的是 agent 自己，不需要申请任何模型 API**：

```bash
# 1. 生成任务包：渲染 PDF 首页为 PNG，并附本地正则提示与统一 prompt
invoice-master agent-prepare ./发票/ -o agent_request.json --images-dir ./agent_images

# 2. agent 用自己的多模态能力查看 agent_request.json 中每个 task.image，
#    按 prompt 抽取字段，写出 agent_answers.json：
#    {"answers": [{"id": "<task.id>", "fields": {"amount": "1060.00", ...}}]}

# 3. 回填为与 extract 一致的标准结果
invoice-master agent-apply agent_request.json agent_answers.json -o extracted.json

# 4. 基于结果直接生成报表，不重复识别
invoice-master report --from-extract extracted.json -o 报销汇总.xlsx
```

要点：

- 本地正则已识别的字段优先，agent 只补空缺（回填时有同样的交叉校验）。
- `--dpi` / `--max-px` 控制图片精度与视觉 token 消耗。
- 注意：agent 自身也是云端模型，图片由当前 agent 的模型处理。

### 提取引擎（`extract` / `report` 的 `--engine`）

| 引擎 | 说明 | 依赖 |
|------|------|------|
| `local`（默认） | 本地正则，10 类票种 | 无 |
| `piaoyihe` | 7 票种工厂（火车/飞机/机动车/出租车/定额/通行费/普票） | PyMuPDF |
| `hybrid` | 本地正则 + 远程视觉兜底 | 自备模型服务 |
| `dual` | 双引擎（OCR → Vision → 本地兜底） | 自备模型服务 |

```bash
invoice-master extract ./发票/ --engine local
invoice-master extract ./发票/ --engine piaoyihe  # 7 票种工厂
```

扫描件优先走上面的 agent 视觉识别，不必启用远程引擎。
远程引擎缺依赖或配置时会自动回退到 `local` 并提示，不会静默失败。
`--enable-llm` 保留为 `--engine hybrid` 的等价写法（向后兼容）。

---

## 🛠️ 命令参考（agent 调用的 CLI）

下面是工具的完整命令面。用户通常只管吩咐 agent；本节供排查、调试或手动执行。

| 命令 | 用途 |
|------|------|
| `extract` | 提取本地 PDF 字段 |
| `agent-prepare` | 生成供 agent 视觉识别的任务包 |
| `agent-apply` | 把 agent 回填的字段 JSON 转成标准提取结果 |
| `merge` | 合并 PDF / 图片为报销单（详见「合并打印速查表」） |
| `report` | 生成 Excel 汇总表（`--from-extract` 可复用识别结果） |
| `rename` | 按模板批量重命名（默认预览） |
| `pair` | 发票与凭证配对检查 |
| `scan` | 扫描邮箱收集附件 |
| `config` | 初始化/查看配置 |
| `layouts` | 列出可用合并布局 |
| `audit` | 严格真值审计 |
| `urlcheck` | 链接发票 URL 的 SSRF 安全校验 |
| `verify` | 产物批次完整性 / SHA256 准入校验 |

全局选项：

```bash
-c, --config PATH   指定配置文件
-v, --verbose       调试日志（前置后置都行）
    --json          JSON 输出（便于脚本集成）
    --no-llm        禁用远程视觉兜底（覆盖配置）
```

> 兼容：改名前的 `invoice-toolkit` 命令仍可用，指向同一入口。

### 合并打印（`merge`）速查表

支持 PDF 与图片（`.png` / `.jpg` / `.jpeg`；图片只参与排版，不做字段提取），
按网格排成可打印的报销单 PDF：一个文件占一格，超出自动换页。

#### 布局（`--layout` / `-l`，默认 `横向2x2`）

6 种预设：

| 预设名 | 纸张方向 | 行 × 列 | 每页张数 | 适合 |
|---|---|---|---|---|
| `横向2x2` | 横向 | 2 × 2 | 4 | 默认，常规发票 |
| `横向2x4` | 横向 | 2 × 4 | 8 | 单据多、省页数 |
| `横向3x3` | 横向 | 3 × 3 | 9 | 小额票密集归档 |
| `竖向1x2` | 竖向 | 2 × 1 | 2 | 火车票、出租车票等细长票 |
| `竖向1x3` | 竖向 | 3 × 1 | 3 | 细长票，更省纸 |
| `竖向2x4` | 竖向 | 4 × 2 | 8 | 竖向密集排布 |

任意行列自定义（1 × 1 – 10 × 10）：

```bash
invoice-master merge ./发票/ -o 报销单.pdf --layout 横向3x5   # 3 行 5 列
invoice-master merge ./发票/ -o 报销单.pdf -l 竖向10x10      # 10 行 10 列
invoice-master merge ./发票/ -o 报销单.pdf -l 竖向10x10 \
    --margin-top 12 --gap 5 --crop-marks --batch-size 20
```

- 写法：`横向RxC` / `竖向RxC`，**R = 行数，C = 列数**，各 1–10
- 兼容英文方向与多种分隔符：`landscape3x5`、`竖向 2x4`（带空格）、`3X5`、`3×5`
- **坑**：预设名优先于动态解析，且三个竖向预设名的数字是「列 × 行」，与动态写法的
  「行 × 列」相反 —— `竖向1x2` 命中预设，实际是 2 行 1 列。要竖向纸 1 行 2 列，
  请写 `portrait1x2`（英文方向可绕过预设名）或 `--rows 1 --cols 2`

#### 排版参数

| 参数 | 单位 | 默认 | 说明 |
|---|---|---|---|
| `--rows` / `--cols` | — | 随布局 | 行数 / 列数，覆盖 `--layout` 的值 |
| `--rotate` | 度 | `0` | 单格内旋转：`0` / `90` / `180` / `270` |
| `--page` | — | `A4` | `A4`(595×842pt) / `A3`(842×1191pt) / `LETTER`(612×792pt) |
| `--margin-top` `--margin-bottom` `--margin-left` `--margin-right` | mm | 各 `10` | 四边页边距 |
| `--gap` | pt（点） | `5` | 发票间隙（注意单位不是 mm） |
| `--crop-marks` | — | 关 | 绘制裁切线 |
| `--crop-mark-left` / `--crop-mark-right` | mm | `0` | 裁切线左右偏移 |
| `--no-dividers` | — | 默认画 | 关闭格子之间的分隔线 |
| `--batch-size` | 个 | `50` | 分批合并再拼接，防几百页耗尽内存；自动对齐到每页格数的整数倍 |
| `--mode` | — | `vector` | 合并模式，见下 |
| `-r` / `--recursive` | — | 关 | 递归扫描子目录 |

#### `--mode` 怎么选

| 取值 | 兼容叫法 | 做法 | 何时用 |
|---|---|---|---|
| `vector` | `普通` | `show_pdf_page` 原样搬页面 | 默认首选：体积小、文字可搜索 |
| `raster` | `图像` | `get_pixmap` 150 DPI 整页转图 | 矢量模式下图层/注释丢失、内容错位时 |

- 监制章（PDF Stamp 注释）在矢量模式下会**自动**以 450 DPI 裁剪贴回，无需手动处理；
  若仍缺失或位置异常，改用 `raster` 重试

#### 参数优先级

1. 命令行显式参数（`--rows`、`--gap`、`--mode` …）
2. `--layout` 解析结果（预设名或动态 `横向RxC`）
3. 配置文件 `merge:` 段（`invoice_master.yaml`）
4. 内置默认值（即 `横向2x2` + 上表默认值）

### 重命名模板占位符

| 占位符 | 示例 |
|--------|------|
| `{date}` | `2026-03-15` |
| `{date_compact}` | `20260315` |
| `{type}` | `住宿发票` |
| `{amount}` | `888.00` |
| `{seller}` | `深圳湾万怡酒店` |
| `{purchaser}` | 购买方名称 |
| `{code}` | 发票号码后 8 位 |
| `{index}` / `{index:03d}` | 序号，自动补零 |

> ⚠️ `rename` 默认是**预览模式**，确认输出无误后再加 `--execute` 真正改名。

### 其他能力

```bash
invoice-master audit --truth-manifest truth.json --run-root ./run
invoice-master urlcheck https://example.com/invoice.pdf        # SSRF 防护
invoice-master verify ./归档/ --truth-manifest truth.json      # 批次准入
```

---

## ⚙️ 配置

生成默认配置：

```bash
invoice-master config --init invoice_master.yaml
```

### 配置优先级

```
内置默认值  <  配置文件  <  环境变量
```

### 敏感信息用环境变量

**不要把密码写进配置文件。** 用环境变量注入：

```bash
# 邮箱授权码（QQ/163 需在邮箱设置里开启 IMAP 后获取）
export INVOICE_MASTER_MAILBOX_PASSWORD=your_auth_code
```

命名规则：`INVOICE_MASTER_<段名>_<字段名>`，例如：

```bash
export INVOICE_MASTER_MAILBOX_HOST=imap.qq.com
```

兼容说明：改名前的 `INVOICE_TOOLKIT_*` 环境变量和 `invoice_toolkit.yaml` 等
旧配置文件名仍可读取；同名项以 `INVOICE_MASTER_*` 和新文件名优先。

### 配置片段

```yaml
mailbox:
  host: imap.qq.com
  port: 993
  username: your@qq.com
  # password 留空，用环境变量注入
  days_back: 180
  read_only: true          # 只读，不改动邮件

filter:
  domain_whitelist: []     # 发件域名白名单，命中即通过筛选
  subject_keywords: ["发票", "行程单", "invoice"]

extraction:
  local_enabled: true      # 本地正则引擎
  confidence_threshold: 0.6

merge:
  # 会被 --layout 与命令行单项参数覆盖，优先级见「参数优先级」
  orientation: landscape
  rows: 2
  cols: 2
  mode: vector             # vector=普通(矢量清晰) | raster=图像(兼容注释)

archive:
  root: ~/Desktop/发票整理
  rename_template: "{date}-{type}-{amount}元"
```

---

## 🧩 票种支持

| 类别 | 覆盖类型 |
|------|----------|
| 交通 | 火车票/铁路电子客票、机票/航班行程单、打车发票、行程单 |
| 住宿 | 住宿发票、住宿水单/账单 |
| 生活 | 餐饮、过路费、定额发票、加油票、停车费 |
| 增值税 | 普通发票、专用发票、数电票 |

新增票种只需继承 `BaseExtractor`：

```python
from invoice_master import BaseExtractor, DocType, register
from invoice_master.extractor import FieldRule


@register
class MyInvoiceExtractor(BaseExtractor):
    keywords = ("我的票种名称",)
    type_name = DocType.OTHER
    priority = 15  # 数字越小越优先

    amount_rules = (FieldRule("金额", (r"金额[:：]\s*([\d.]+)",)),)
    date_rules = (FieldRule("日期", (r"日期[:：]\s*(\d{4}-\d{2}-\d{2})",)),)
```

---

## 🧪 开发

```bash
pip install -e ".[dev]"

pytest              # 跑测试（162 个用例）
pytest --cov=invoice_master --cov-report=term-missing
ruff check .        # 代码检查
ruff format .       # 格式化
```

### 项目结构

```
SKILL.md               agent 执行指令（入口）
scripts/invoice_master/
├── config.py       配置系统（三层覆盖 + 脱敏）
├── models.py       领域模型、Decimal 解析、配对算法
├── pdf.py          PyMuPDF 封装、版面分栏
├── extractor.py    提取引擎（工厂 + 策略）
├── rules.py        各票种提取规则
├── agent_bridge.py Agent 视觉识别两段式握手
├── llm.py          远程视觉兜底 + 熔断器
├── mailbox.py      IMAP 抓取 + 四层筛选漏斗
├── merger.py       PDF 合并
├── reporter.py     重命名 + Excel 报表
└── cli.py          命令行入口

tests/
├── test_extractor.py  字段提取 + 解析（71）
├── test_mailbox.py    邮箱筛选 + IMAP 集成（30）
├── test_merger.py     PDF 合并 + 监制章回归（29）
└── test_cli.py        CLI + 配置 + 默认行为约束（32）
```

### CI

`.github/workflows/ci.yml` 在推送和 PR 时自动运行：
- 跨平台测试矩阵（Ubuntu / Windows / macOS × Python 3.9-3.12）
- Ruff 代码检查与格式校验
- 构建分发包并冒烟验证 CLI

---

## ⚠️ 免责声明

* 本工具通过 IMAP **只读**访问邮箱，不发送、删除或修改任何邮件。使用者须确保对所处理邮箱拥有合法授权。
* 若启用可选的远程识别引擎，发票图片会发送至第三方模型服务（默认关闭）；agent 视觉识别则走当前 agent 的模型通道。按实际需求决定。
* 本工具不保证识别结果的准确性或财务合规性。**报销前请人工核验。**
* 作者不对使用本工具造成的损失或数据丢失承担责任。

---

## 🔌 已知限制：IMAP SEARCH 的中文问题

部分邮箱服务器（实测 QQ 邮箱）**不支持在 SEARCH 命令里传中文**，会报：

```
ascii codec can't encode characters ... ordinal not in range(128)
```

所以本工具**不使用 IMAP 端关键词搜索**，改为在客户端拉取后本地过滤。
代价是要下载全部邮件正文（`BODY.PEEK[]`，不标记已读），
好处是行为可预测 —— 邮箱服务器可能悄悄忽略不支持的搜索条件。

日期搜索 `SINCE` / `BEFORE` 是可用的（ASCII），但同样建议以客户端日期
过滤为准：部分服务器在 `SINCE` 语义不标准时可能返回超出范围的结果。

需要搜索历史邮件里的关键词时，用：

```bash
# 拉取后本地过滤（支持中文）
invoice-master scan -o ./out --days 365
```

### 实测数据范围参考

以某 QQ 邮箱为例，各文件夹的实际可访问范围：

| 文件夹 | 邮件数 | 日期范围 |
|---|---|---|
| INBOX | 199 | 2026-09-16 ~ 2026-10-16 |
| Sent Messages | 1 | 2026-10-13 |
| Deleted Messages | 0 | — |
| Junk | 9 | 2026-09-17 ~ 2026-09-29 |

注意：**IMAP 能看到的历史范围不一定等于网页端能看到的范围**，
部分邮箱只同步最近一段时间。若网页端有更早的邮件但 IMAP 拉不到，
需要在邮箱设置里开启「IMAP 收取全部邮件」。

---

## 📄 许可证

本项目采用 [Apache License 2.0](LICENSE)。第三方署名见 [NOTICE](NOTICE)。
