"""配置系统。

三层配置优先级（后者覆盖前者）：
1. 内置默认值（DEFAULT_CONFIG）
2. 配置文件（YAML / JSON）
3. 环境变量（INVOICE_MASTER_* 前缀）

设计上刻意不引入 pydantic —— 保持零重依赖，让使用者clone 下来就能跑。
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    # 邮箱抓取 -------------------------------------------------------------
    "mailbox": {
        # IMAP 服务器；常见值：imap.qq.com / imap.163.com
        "host": "",
        "port": 993,
        "username": "",
        # 授权码而非登录密码。强烈建议留空并用环境变量注入：
        #   INVOICE_MASTER_MAILBOX_PASSWORD
        "password": "",
        "use_ssl": True,
        # 只读访问，绝不改动邮箱
        "read_only": True,
        # 扫描最近多少天；部分邮箱默认只保留近 30 天邮件
        "days_back": 180,
        # 邮件正文最大读取字节数，避免超大邮件拖慢扫描
        "max_body_bytes": 262144,
        # 连接与操作超时（秒）
        "timeout": 30,
    },
    # 邮件筛选漏斗 ---------------------------------------------------------
    "filter": {
        # 第一层：发件域 / 正文域名白名单。命中即通过，不再往下判。
        # 留空表示不启用该层。
        "domain_whitelist": [],
        # 第一层补充：发件人地址精确白名单
        "sender_whitelist": [],
        # 第二层：主题关键字（任一命中即通过，大小写不敏感）
        # 「票据」覆盖停车电子票据、财政票据等非税收入票据；
        # 「数电发票 / 数电票」覆盖全面数字化的电子发票邮件。
        "subject_keywords": [
            "发票",
            "电子发票",
            "票据",
            "数电发票",
            "数电票",
            "行程单",
            "订单",
            "账单",
            "水单",
            "invoice",
            "receipt",
            "itinerary",
            "booking",
        ],
        # 第三层：正文关键字（命中任一即通过）
        "body_keywords": [
            "发票",
            "电子发票",
            "票据",
            "数电发票",
            "数电票",
            "增值税",
            "行程单",
            "报销",
            "订单",
            "invoice",
            "tax",
            "itinerary",
        ],
        # 明确排除的主题关键字（优先于一切白名单判断）
        "exclude_subject_keywords": [
            "验证码",
            "登录",
            "密码",
            "安全验证",
            "账户变更",
            "广告",
            "推广",
            "优惠",
            "营销",
        ],
        # 附件过滤
        "accepted_extensions": [".pdf", ".ofd", ".xml"],
        # ZIP 内递归解包的最大深度，防止 zip 炸弹
        "max_archive_depth": 2,
        # 单封邮件附件大小上限（MB），超限暂存但不处理
        "max_attachment_mb": 20,
        # 小于该边长的图片视为图标/追踪像素，直接丢弃（像素）
        "min_image_size_px": 32,
    },
    # 字段提取 -------------------------------------------------------------
    "extraction": {
        # 本地正则引擎开关（默认开）
        "local_enabled": True,
        # PDF 文本层最大解析页数，防止超大 PDF 卡死
        "max_pdf_pages": 20,
        # 低于该置信度的结果标记为需人工复核
        "confidence_threshold": 0.6,
        # 提取失败时是否回退到文件修改时间作为日期
        "fallback_to_mtime": True,
    },
    # LLM 兜底 -------------------------------------------------------------
    "llm": {
        # 默认关闭 —— 正则优先，LLM 只在兜底时才需要
        "enabled": False,
        # OpenAI 兼容端点，兼容 OpenAI / 智谱 / DeepSeek / 通义 等
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v",
        # 密钥务必用环境变量：INVOICE_MASTER_LLM_API_KEY
        "api_key": "",
        "timeout": 60,
        "max_retries": 2,
        # 连续失败多少次后熔断，停止后续调用
        "breaker_threshold": 3,
        # 单次运行最多调用次数，防止意外烧钱
        "max_calls_per_run": 200,
        # 传给模型的图片最大边长（像素），控制 token 消耗
        "max_image_px": 1600,
    },
    # 归档 -----------------------------------------------------------------
    "archive": {
        "root": "~/Desktop/发票整理",
        # 按票据类型分文件夹
        "group_by_type": True,
        # 非目标公司发票隔离
        "company_filter": "",
        # 无法可靠识别的进入人工复核队列
        "manual_review_folder": "待人工复核",
        "non_target_folder": "非目标公司发票",
        # 是否重命名文件
        "rename_enabled": True,
        # 重命名模板，可用占位符见 report/renamer.py
        "rename_template": "{date}-{type}-{amount}元",
        # 目标公司匹配为子串包含（用户填简称即可，如 "诺华"）
        "company_match_mode": "contains",
    },
    # 合并 -----------------------------------------------------------------
    "merge": {
        "orientation": "landscape",
        "rows": 2,
        "cols": 2,
        "rotate": 0,
        # 页面方向；"landscape"=横向，"portrait"=纵向
        "page": "A4",
        # 页边距（毫米）
        "margin_top": 10,
        "margin_bottom": 10,
        "margin_left": 10,
        "margin_right": 10,
        # "vector"=矢量嵌入（清晰、小体积），"raster"=转图（保注释、体积大）
        "mode": "vector",
        # 每批处理文件数，避免单文档过大导致内存耗尽
        "batch_size": 50,
        # 发票之间的间隙（点）
        "gap": 5,
        # 是否绘制分割线
        "draw_dividers": True,
        # 是否绘制裁切线
        "show_crop_marks": False,
        "crop_mark_left": 0,
        "crop_mark_right": 0,
    },
    # 报表 -----------------------------------------------------------------
    "report": {
        "filename": "summary_report.xlsx",
        # 金额小数位
        "amount_precision": 2,
        # 高亮重复的发票号码
        "highlight_duplicates": True,
        # 重复行填充色（浅红）
        "duplicate_fill": "FFF2CC",
    },
}

# 环境变量覆盖映射：ENV 前缀 + 路径.键名
ENV_PREFIX = "INVOICE_MASTER_"
# 兼容改名前的环境变量；同名配置项以 INVOICE_MASTER_* 为准。
LEGACY_ENV_PREFIX = "INVOICE_TOOLKIT_"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典，override 中的标量直接覆盖 base。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _coerce(raw: str) -> Any:
    """把环境变量字符串转成合适的 Python 类型。"""
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", ""}:
        return None if lowered != "" else ""
    # 尝试转数字
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        pass
    # 尝试 JSON（支持列表等复杂值）
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _apply_env_overrides(
    config: dict[str, Any], environ: dict[str, str]
) -> dict[str, Any]:
    """应用环境变量覆盖。

    命名规则：INVOICE_MASTER_<SECTION>_<KEY>，例如
        INVOICE_MASTER_MAILBOX_HOST=imap.qq.com
        INVOICE_MASTER_LLM_ENABLED=true

    兼容改名前的 ``INVOICE_TOOLKIT_*``；先应用旧前缀，再应用新前缀，
    因此两者同时存在时以新前缀为准。
    """
    result = copy.deepcopy(config)
    for prefix in (LEGACY_ENV_PREFIX, ENV_PREFIX):
        for env_key, env_value in environ.items():
            if not env_key.startswith(prefix):
                continue
            remainder = env_key[len(prefix) :].lower()
            # 在默认配置里找匹配 section.key
            for section_name, section_value in DEFAULT_CONFIG.items():
                if not isinstance(section_value, dict):
                    continue
                section_prefix = f"{section_name}_"
                if not remainder.startswith(section_prefix):
                    continue
                field_name = remainder[len(section_prefix) :]
                if field_name in section_value:
                    result[section_name][field_name] = _coerce(env_value)
                    break
    return result


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """配置对象。

    用点号路径访问嵌套配置::

        cfg = load_config()
        cfg.get("merge.rows")          # -> 2
        cfg.get("llm.enabled")         # -> False
    """

    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG))
    source_path: Path | None = None

    def get(self, path: str, default: Any = None) -> Any:
        """按点号路径读取配置，如 ``"mailbox.port"``。"""
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        """按点号路径写入配置，支持创建不存在的中间层。"""
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    def section(self, name: str) -> dict[str, Any]:
        """获取整个配置段的可变引用。"""
        value = self.data.get(name)
        if not isinstance(value, dict):
            value = {}
            self.data[name] = value
        return value

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def is_llm_enabled(self) -> bool:
        """LLM 兜底是否真正可用 —— 开关打开且密钥已配置。"""
        return bool(self.get("llm.enabled")) and bool(self.get("llm.api_key"))


def _find_default_config_file(start: Path | None = None) -> Path | None:
    """在工作目录及其父目录中查找默认配置文件。

    查找顺序：新文件名优先，兼容改名前的 ``invoice_toolkit.*``。
    """
    names = (
        "invoice_master.yaml",
        "invoice_master.yml",
        "invoice_master.json",
        "invoice_toolkit.yaml",
        "invoice_toolkit.yml",
        "invoice_toolkit.json",
    )
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(
    config_path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    apply_env: bool = True,
    search_parents: bool = True,
) -> Config:
    """加载配置。

    Args:
        config_path: 显式指定的配置文件路径；为 None 时向上查找
        overrides: 运行时覆盖字典（点号路径 -> 值），优先级最高
        apply_env: 是否应用环境变量覆盖
        search_parents: 是否向父目录查找配置文件

    Returns:
        Config 实例

    Raises:
        FileNotFoundError: 显式指定但文件不存在
        ValueError: 配置文件格式错误
    """
    path: Path | None = None
    if config_path is not None:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"配置文件不存在: {path}")
    elif search_parents:
        path = _find_default_config_file()

    data = copy.deepcopy(DEFAULT_CONFIG)

    if path is not None:
        data = _deep_merge(data, _read_config_file(path))

    if apply_env:
        data = _apply_env_overrides(data, dict(os.environ))

    if overrides:
        data = _deep_merge(data, overrides)

    return Config(data=data, source_path=path)


def _read_config_file(path: Path) -> dict[str, Any]:
    """读取 YAML 或 JSON 配置文件。

    YAML 解析为可选依赖 —— 没装 PyYAML 时给出明确的安装提示，
    而不是抛一个看不懂的 ImportError。
    """
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix == ".json":
        try:
            return json.loads(text) or {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 配置解析失败 ({path}): {exc}") from exc

    # YAML（.yaml / .yml / 无后缀）
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        # 没有 PyYAML 时，尝试用 JSON 解析（YAML 是 JSON 的超集）
        try:
            return json.loads(text) or {}
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"解析 YAML 配置需要 PyYAML，请执行 `pip install pyyaml`；"
                f"或改用 JSON 格式。\n原始错误: {exc}"
            ) from exc

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 配置解析失败 ({path}): {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"配置文件根节点必须是映射/字典 ({path})")
    return loaded


def save_config(config: Config, path: str | Path, *, fmt: str | None = None) -> Path:
    """保存配置到文件。

    Args:
        config: 要保存的 Config
        path: 目标路径
        fmt: 强制指定格式 "yaml" / "json"；默认按后缀推断
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fmt = fmt or ("json" if target.suffix.lower() == ".json" else "yaml")

    if fmt == "json":
        target.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ValueError(
            "保存 YAML 配置需要 PyYAML，请执行 `pip install pyyaml`，或改用 .json 后缀。"
        ) from exc

    header = (
        "# invoice-master 配置\n"
        "# 文档: https://cnb.cool/52hz.ltd/Skills/invoice-master\n"
        "# 环境变量可覆盖任意配置项，例如 INVOICE_MASTER_MAILBOX_HOST\n"
    )
    body = yaml.safe_dump(config.to_dict(), allow_unicode=True, sort_keys=False, indent=2)
    target.write_text(header + body, encoding="utf-8")
    return target


def redact(config: Config) -> dict[str, Any]:
    """返回脱敏后的配置副本，用于日志和错误信息。

    密码、API Key 等敏感字段一律替换为 ``***``。
    """
    import re

    sensitive = re.compile(r"(password|api_key|secret|token|authorization)", re.I)
    data = config.to_dict()

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: ("***" if sensitive.search(str(k)) and v else walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(data)
