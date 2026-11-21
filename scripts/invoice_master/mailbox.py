"""IMAP 邮件抓取与筛选。

对标参考项目 Invoice-Downloader 的 ``email_fetcher.py``，保留其核心设计——
**四层漏斗**，因为对每封邮件都跑OCR/AI 的成本和误报都不可接受：

    ① 发件域名 / 发件人白名单  ── 命中即通过，不再往下判
    ② 主题关键字
    ③ 正文关键字
    ④ 附件类型 + 体积 + 图片尺寸

安全约束：
- **只读**，永不改动邮箱（不标记已读、不移动、不删除）
- 凭据只从配置/环境变量取，不落日志（经 :func:`config.redact` 脱敏）
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from io import BytesIO
from pathlib import Path

from .config import Config, redact

logger = logging.getLogger(__name__)


class MailboxError(RuntimeError):
    """邮箱连接或操作异常。"""


# ---------------------------------------------------------------------------
# 邮件筛选漏斗
# ---------------------------------------------------------------------------


#: 发票类附件扩展名。
#:
#: 这个常量是「筛掉噪音」的关键 —— 实测一个技术人的收件箱里
#: Medium/LinkedIn 简报正文常含「账单」二字会被关键字层放行，
#: 但它们**没有任何附件**。所以除了正文关键字，还必须有发票类附件。
INVOICE_EXTENSIONS = frozenset({".pdf", ".ofd", ".xml", ".zip", ".rar", ".7z"})


def is_invoice_attachment(filename: str) -> bool:
    """文件名是否是发票类附件。

    刻意只看**扩展名**不看内容 —— 需要读内容才能判断的话，
    就得把所有邮件的附件全下载一遍，代价与风险都不成比例。
    """
    suffix = Path(filename).suffix.lower()
    return suffix in INVOICE_EXTENSIONS


@dataclass
class MailFilter:
    """四层筛选漏斗。

    用法::

        f = MailFilter.from_config(cfg)
        if f.should_scan(message, body_text, attachment_names=attachment_names):
            ...  # 这封邮件值得处理
    """

    domain_whitelist: set[str] = field(default_factory=set)
    sender_whitelist: set[str] = field(default_factory=set)
    subject_keywords: tuple[str, ...] = ()
    body_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    accepted_extensions: set[str] = field(
        default_factory=lambda: {".pdf", ".ofd", ".xml"}
    )
    max_body_bytes: int = 262144
    min_image_size_px: int = 32

    @classmethod
    def from_config(cls, config: Config) -> MailFilter:
        section = config.section("filter")
        return cls(
            domain_whitelist={
                d.strip().lower().lstrip("@")
                for d in section.get("domain_whitelist", [])
                if str(d).strip()
            },
            sender_whitelist={
                s.strip().lower()
                for s in section.get("sender_whitelist", [])
                if str(s).strip()
            },
            subject_keywords=tuple(section.get("subject_keywords", ())),
            body_keywords=tuple(section.get("body_keywords", ())),
            exclude_keywords=tuple(section.get("exclude_subject_keywords", ())),
            accepted_extensions={
                str(e).lower() if str(e).startswith(".") else f".{str(e).lower()}"
                for e in section.get("accepted_extensions", [".pdf", ".ofd", ".xml"])
            },
            max_body_bytes=int(section.get("max_body_bytes", 262144)),
            min_image_size_px=int(section.get("min_image_size_px", 32)),
        )

    def should_scan(
        self,
        message: Message,
        body: str = "",
        attachment_names: Sequence[str] | None = None,
    ) -> tuple[bool, str]:
        """判断邮件是否值得处理。

        Args:
            message: 邮件对象
            body: 已提取的正文
            attachment_names: 该邮件的附件文件名列表。

                **传这个参数能挡掉大量噪音**：实测技术类收件箱里，
                Medium / LinkedIn 简报的正文常含「账单」「发票」等词，
                只靠关键字会大量误判。但它们一个附件都没有。
                有附件名时，除白名单外还要求存在发票类附件。

        Returns:
            (是否通过, 判定原因)
        """
        subject = decode_str(message.get("Subject", ""))
        from_addr = extract_address(message.get("From", ""))

        # 排除词优先级最高：验证码/广告邮件再像发票也不处理
        lowered_subject = subject.lower()
        for keyword in self.exclude_keywords:
            if keyword.lower() in lowered_subject:
                return False, f"subject_excluded:{keyword}"

        # 白名单放行在最前 —— 用户显式指定的域不该被附件检查拦下
        if self.sender_whitelist and from_addr in self.sender_whitelist:
            return True, "sender_whitelist"
        if self.domain_whitelist:
            domain = from_addr.rpartition("@")[2]
            if domain and any(
                domain == d or domain.endswith(f".{d}") for d in self.domain_whitelist
            ):
                return True, "domain_whitelist"

        # 附件前置检查。
        # 实测技术类收件箱里，Medium / LinkedIn 简报正文常含「账单」
        # 「发票」等词，只靠关键字会大量误判——但它们一个附件都没有。
        # 有附件信息时，先确认这封邮件确实带发票类文件。
        if attachment_names is not None and not any(
            is_invoice_attachment(name) for name in attachment_names
        ):
            return False, "no_invoice_attachment"

        # 第二层：主题关键字
        if subject and self._hits(lowered_subject, self.subject_keywords):
            return True, "subject_keyword"

        # 第三层：正文关键字
        if body and self._hits(body.lower(), self.body_keywords):
            return True, "body_keyword"

        return False, "no_match"

    @staticmethod
    def _hits(haystack: str, keywords: Iterable[str]) -> bool:
        return any(k.lower() in haystack for k in keywords if k)

    def is_accepted_attachment(self, filename: str, payload: bytes) -> tuple[bool, str]:
        """附件是否值得处理。

        丢弃追踪像素/图标，暂存超大文件，放行正常发票。
        """
        suffix = Path(filename).suffix.lower()

        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp"}:
            # 图片：太小的多为追踪像素或logo
            if _image_too_small(payload, self.min_image_size_px):
                return False, "image_too_small"
            return True, "image"

        if suffix and suffix not in self.accepted_extensions:
            return False, f"ext_rejected:{suffix}"
        if not suffix:
            return False, "no_extension"
        return True, "ok"


def _image_too_small(payload: bytes, min_px: int) -> bool:
    """判断图片是否过小（不引入 PIL，靠解析头部尺寸）。"""
    if len(payload) < 1024:
        return True  # 1KB 以下几乎必然是图标

    try:
        from PIL import Image  # type: ignore[import-untyped]

        with Image.open(BytesIO(payload)) as img:
            return max(img.size) < min_px
    except ImportError:
        # 没装 Pillow 时只看体积，不误杀正常图片
        return len(payload) < 4 * 1024
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 附件解压
# ---------------------------------------------------------------------------


def iter_archive(payload: bytes, max_depth: int = 2) -> Iterator[tuple[str, bytes]]:
    """递归解包 ZIP 附件。

    Yields:
        (内部文件名, 内容)

    防御 zip 炸弹：限制递归深度，且单条目解压体积上限 100MB。
    """
    max_entry_size = 100 * 1024 * 1024

    def walk(data: bytes, prefix: str, depth: int) -> Iterator[tuple[str, bytes]]:
        if depth > max_depth:
            logger.warning("附件嵌套超过 %d 层，停止解包: %s", max_depth, prefix)
            return
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if info.file_size > max_entry_size:
                        logger.warning("压缩包内条目过大，跳过: %s", info.filename)
                        continue
                    try:
                        content = archive.read(info)
                    except (RuntimeError, zipfile.BadZipFile) as exc:
                        logger.warning("解压失败 %s: %s", info.filename, exc)
                        continue
                    name = f"{prefix}/{info.filename}" if prefix else info.filename
                    if info.filename.lower().endswith(".zip"):
                        yield from walk(content, name, depth + 1)
                    else:
                        yield name, content
        except zipfile.BadZipFile:
            return

    yield from walk(payload, "", 1)


# ---------------------------------------------------------------------------
# IMAP 客户端
# ---------------------------------------------------------------------------


@dataclass
class FetchedAttachment:
    """从邮件中取出的一个附件。"""

    filename: str
    payload: bytes
    message_uid: str
    subject: str = ""
    sender: str = ""
    received_at: datetime | None = None

    def save_to(self, directory: Path, *, dry_run: bool = False) -> Path | None:
        """保存附件到目录，返回落盘路径。"""
        target = directory / self.filename
        if dry_run:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return target


@dataclass
class ScanStats:
    """一次扫描的统计。"""

    scanned: int = 0
    matched: int = 0
    attachments: int = 0
    skipped: int = 0
    errors: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note_reason(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_text(self) -> str:
        lines = [
            f"扫描邮件: {self.scanned}",
            f"命中筛选: {self.matched}",
            f"提取附件: {self.attachments}",
            f"跳过: {self.skipped}",
            f"错误: {self.errors}",
        ]
        if self.reasons:
            top = sorted(self.reasons.items(), key=lambda kv: -kv[1])[:5]
            lines.append("跳过原因 TOP5: " + ", ".join(f"{k}={v}" for k, v in top))
        return "\n".join(lines)


class ImapFetcher:
    """IMAP 只读抓取器。

    安全：全程只读，不设置 ``\\Seen`` 标志、不移动/删除邮件。
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.filter = MailFilter.from_config(config)
        section = config.section("mailbox")
        self.host = str(section.get("host", "")).strip()
        self.port = int(section.get("port", 993))
        self.username = str(section.get("username", "")).strip()
        self.password = str(section.get("password", ""))
        self.use_ssl = bool(section.get("use_ssl", True))
        self.timeout = int(section.get("timeout", 30))
        self.days_back = int(section.get("days_back", 180))
        self.read_only = bool(section.get("read_only", True))
        self._connection: imaplib.IMAP4 | None = None

    # -- 连接管理 ---------------------------------------------------------

    def validate(self) -> None:
        """检查配置完整性。"""
        missing = []
        if not self.host:
            missing.append("mailbox.host")
        if not self.username:
            missing.append("mailbox.username")
        if not self.password:
            missing.append(
                "mailbox.password（或环境变量 INVOICE_MASTER_MAILBOX_PASSWORD）"
            )
        if missing:
            safe = redact(self.config)
            raise MailboxError(
                "邮箱配置缺失: "
                + ", ".join(missing)
                + f"\n当前配置: {safe.get('mailbox')}"
            )

    def connect(self) -> imaplib.IMAP4:
        """建立连接并登录。"""
        self.validate()
        try:
            if self.use_ssl:
                conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    self.host, self.port, timeout=self.timeout
                )
            else:
                conn = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
                conn.starttls()
            conn.login(self.username, self.password)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(
                f"邮箱认证失败: {exc}\n提示：QQ/163 邮箱需要填写「授权码」而非登录密码。"
            ) from exc
        except OSError as exc:
            raise MailboxError(f"无法连接 {self.host}:{self.port} — {exc}") from exc

        self._connection = conn
        logger.info("已连接邮箱 %s", self.host)
        return conn

    def close(self) -> None:
        """关闭连接。"""
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
            try:
                self._connection.logout()
            except Exception:
                pass
            self._connection = None

    def __enter__(self) -> ImapFetcher:
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    # -- 扫描 -------------------------------------------------------------

    def scan(
        self,
        *,
        mailbox: str = "INBOX",
        days_back: int | None = None,
        dry_run: bool = False,
        limit: int = 0,
    ) -> tuple[list[FetchedAttachment], ScanStats]:
        """扫描邮箱，收集疑似发票附件。

        Args:
            mailbox: 邮箱文件夹名
            days_back: 回溯天数，默认取配置
            dry_run: 只统计不落盘
            limit: 最多处理的邮件数，0 表示不限

        Returns:
            (附件列表, 统计)
        """
        if self._connection is None:
            self.connect()
        conn = self._connection
        assert conn is not None

        days = days_back if days_back is not None else self.days_back
        since = datetime.now() - timedelta(days=days)
        stats = ScanStats()
        attachments: list[FetchedAttachment] = []

        try:
            conn.select(mailbox, readonly=self.read_only)
            criteria = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
            status, data = conn.search(None, "ALL", criteria)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"查询邮件失败: {exc}") from exc

        if status != "OK" or not data or not data[0]:
            logger.warning("没有找到 %s 天的邮件", days)
            return attachments, stats

        uids = data[0].split()
        if limit:
            uids = uids[-limit:]

        logger.info("待扫描 %d 封邮件（最近 %d 天）", len(uids), days)

        for uid in uids:
            if stats.scanned >= limit > 0:
                break
            stats.scanned += 1
            try:
                found = self._process_message(uid, stats, dry_run)
                attachments.extend(found)
            except Exception as exc:
                stats.errors += 1
                logger.warning(
                    "处理邮件 UID %s 失败: %s", uid.decode(errors="ignore"), exc
                )

        stats.matched = sum(1 for _ in attachments) and stats.matched or stats.matched
        logger.info("扫描完成\n%s", stats.as_text())
        return attachments, stats

    def _process_message(
        self, uid: bytes, stats: ScanStats, dry_run: bool
    ) -> list[FetchedAttachment]:
        """处理单封邮件：筛选 -> 提取附件。"""
        conn = self._connection
        assert conn is not None

        status, data = conn.fetch(uid, "(BODY.PEEK[])")  # PEEK 保证不标记已读
        if status != "OK" or not data:
            return []

        raw = next((part for part in data if isinstance(part, tuple)), None)
        if raw is None:
            return []

        message = email.message_from_bytes(raw[1])
        body = extract_body(message, self.filter.max_body_bytes)

        # 先取附件名 —— 供筛选层判断"这封邮件到底有没有发票类文件"，
        # 避免纯文本简报因正文含「账单」二字被误判为发票邮件
        attachment_names = [
            decode_str(part.get_filename())
            for part in message.walk()
            if part.get_filename()
        ]

        passed, reason = self.filter.should_scan(
            message, body, attachment_names=attachment_names
        )
        if not passed:
            stats.skipped += 1
            stats.note_reason(reason)
            return []
        stats.matched += 1
        logger.debug("命中邮件 %s（%s）", decode_str(message.get("Subject", "")), reason)

        uid_str = uid.decode(errors="ignore")
        sender = extract_address(message.get("From", ""))
        received = parse_date_header(message.get("Date", ""))

        results: list[FetchedAttachment] = []
        for filename, payload in iter_attachments(message, self.filter.max_body_bytes):
            accepted, why = self.filter.is_accepted_attachment(filename, payload)
            if not accepted:
                stats.skipped += 1
                stats.note_reason(why)
                continue

            stats.attachments += 1
            results.append(
                FetchedAttachment(
                    filename=filename,
                    payload=payload,
                    message_uid=uid_str,
                    subject=decode_str(message.get("Subject", "")),
                    sender=sender,
                    received_at=received,
                )
            )

            # ZIP 内嵌发票
            if filename.lower().endswith(".zip"):
                max_depth = int(self.config.get("filter.max_archive_depth", 2))
                for inner_name, inner_payload in iter_archive(payload, max_depth):
                    accepted, why = self.filter.is_accepted_attachment(
                        inner_name, inner_payload
                    )
                    if accepted:
                        stats.attachments += 1
                        results.append(
                            FetchedAttachment(
                                filename=f"{Path(filename).stem}_{Path(inner_name).name}",
                                payload=inner_payload,
                                message_uid=uid_str,
                                subject=decode_str(message.get("Subject", "")),
                                sender=sender,
                                received_at=received,
                            )
                        )
                    else:
                        stats.skipped += 1
                        stats.note_reason(f"archive:{why}")

        return results

    def save_all(
        self,
        attachments: Iterable[FetchedAttachment],
        target_dir: str | Path,
        *,
        dry_run: bool = False,
    ) -> list[Path]:
        """把附件批量落盘，按扩展名分子目录。"""
        root = Path(target_dir).expanduser()
        saved: list[Path] = []

        for attachment in attachments:
            subdir = root / Path(attachment.filename).suffix.lstrip(".").lower()
            path = attachment.save_to(subdir, dry_run=dry_run)
            if path is not None:
                saved.append(path)

        if not dry_run:
            logger.info("已保存 %d 个附件到 %s", len(saved), root)
        return saved


# ---------------------------------------------------------------------------
# 邮件解析辅助
# ---------------------------------------------------------------------------


def decode_str(value: str | None) -> str:
    """解码 MIME 编码的邮件头。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def extract_address(value: str | None) -> str:
    """从 ``"Name <a@b.com>"`` 中取出纯邮箱地址。"""
    text = decode_str(value)
    match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    return match.group(0).lower() if match else text.lower()


def extract_body(message: Message, max_bytes: int) -> str:
    """提取纯文本正文（跳过 HTML 与附件）。"""
    parts: list[str] = []
    total = 0

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() != "text/plain":
            continue
        if part.get_filename():
            continue  # 附件而非正文

        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue

        chunk = payload[: max_bytes - total].decode("utf-8", errors="ignore")
        parts.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break

    return "\n".join(parts)


def iter_attachments(message: Message, max_bytes: int) -> Iterator[tuple[str, bytes]]:
    """遍历邮件附件。

    Yields:
        (文件名, 内容)
    """
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue

        filename = decode_str(part.get_filename())
        if not filename:
            continue

        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue

        # 中文文件名兜底：从 Content-Type 的 name 参数取
        if not Path(filename).suffix:
            fallback = decode_str(part.get_param("name", header="content-type"))
            if fallback:
                filename = fallback

        yield filename, payload


def parse_date_header(value: str | None) -> datetime | None:
    """解析邮件 Date 头。"""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value)
    except Exception:
        return None
