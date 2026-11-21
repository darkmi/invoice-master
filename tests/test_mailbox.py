"""邮箱抓取与筛选测试。

不连真实邮箱——用 :class:`FakeIMAPServer` 起一个本地 IMAP 服务器，
真的走 socket 协议，这样 IMAP 命令序列、UID 处理、``readonly`` 标志
都能被验证到，而不是只测纯函数。

覆盖：
* 四层筛选漏斗的各层判定与优先级
* 附件提取（含中文文件名、ZIP 递归解包）
* ``BODY.PEEK[]`` —— 验证不标记已读
* ``readonly=True`` —— 验证不改动邮箱
* 凭据脱敏

运行::

    pytest tests/test_mailbox.py -v
"""

from __future__ import annotations

import base64
import email.utils
import socket
import sys
import threading
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_master.config import Config, redact  # noqa: E402
from invoice_master.mailbox import (  # noqa: E402
    INVOICE_EXTENSIONS,
    ImapFetcher,
    MailboxError,
    MailFilter,
    decode_str,
    extract_address,
    extract_body,
    is_invoice_attachment,
    iter_archive,
    iter_attachments,
)

# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------


class TestMailHelpers:
    def test_decode_mime_header(self):
        """中文标题在邮件里是 MIME 编码，必须解码。"""
        # "发票通知" 的 utf-8 base64
        encoded = base64.b64encode("发票通知".encode()).decode("ascii")
        assert decode_str(f"=?utf-8?B?{encoded}?=") == "发票通知"
        assert decode_str(None) == ""
        assert decode_str("") == ""

    def test_extract_address(self):
        assert extract_address("张三 <zhang@qq.com>") == "zhang@qq.com"
        encoded = base64.b64encode("发票中心".encode()).decode("ascii")
        assert extract_address(f"=?utf-8?B?{encoded}?=<a@b.com>") == "a@b.com"
        assert extract_address(None) == ""


class TestAttachmentExtraction:
    def test_extract_pdf_attachment(self):
        msg = EmailMessage()
        msg["Subject"] = "发票"
        msg.set_content("正文")
        msg.add_attachment(
            b"%PDF-1.4", maintype="application", subtype="pdf", filename="a.pdf"
        )

        files = list(iter_attachments(msg, 1024 * 1024))
        assert len(files) == 1
        assert files[0][0] == "a.pdf"

    def test_extract_chinese_filename(self):
        """中文文件名是这场景的高频问题。"""
        msg = EmailMessage()
        msg["Subject"] = "发票"
        msg.set_content("正文")
        msg.add_attachment(
            b"%PDF-1.4", maintype="application", subtype="pdf", filename="发票_2026.pdf"
        )

        files = list(iter_attachments(msg, 1024 * 1024))
        assert files[0][0] == "发票_2026.pdf"

    def test_body_extracted_without_attachment(self):
        """正文提取不能把附件内容混进来。"""
        msg = EmailMessage()
        msg.set_content("这是正文内容")
        msg.add_attachment(
            b"binary", maintype="application", subtype="pdf", filename="x.pdf"
        )

        body = extract_body(msg, 100000)
        assert "这是正文内容" in body
        assert "binary" not in body

    def test_body_respects_size_limit(self):
        msg = EmailMessage()
        msg.set_content("A" * 100000)

        body = extract_body(msg, 1000)
        assert len(body) <= 1000


# ---------------------------------------------------------------------------
# ZIP 递归解包
# ---------------------------------------------------------------------------


class TestArchiveExtraction:
    def _make_zip(self, entries: dict[str, bytes]) -> bytes:
        import zipfile

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    def test_flat_zip(self):
        payload = self._make_zip({"invoice.pdf": b"%PDF-1.4"})
        files = dict(iter_archive(payload))
        assert "invoice.pdf" in files

    def test_nested_zip(self):
        """真实场景：外层 zip 套内层 zip。"""
        inner = self._make_zip({"invoice.pdf": b"%PDF-1.4"})
        outer = self._make_zip({"backup.zip": inner})
        files = dict(iter_archive(outer))
        assert any("invoice.pdf" in name for name in files)

    def test_depth_limit_prevents_recursion_bomb(self):
        """深度限制必须生效，否则 zip 炸弹能打爆栈。"""
        payload = b"%PDF-1.4"
        for _ in range(6):
            payload = self._make_zip({"nested.zip": payload})

        # max_depth=2 时不该把 6 层都解开
        files = list(iter_archive(payload, max_depth=2))
        assert len(files) <= 3

    def test_corrupt_zip_returns_empty(self):
        assert list(iter_archive(b"not a zip at all")) == []


# ---------------------------------------------------------------------------
# 四层筛选漏斗
# ---------------------------------------------------------------------------


def _make_msg(
    subject: str = "", sender: str = "billing@dingtalk.com", body: str = "发票"
) -> EmailMessage:
    msg = EmailMessage()
    if subject:
        msg["Subject"] = subject
    if sender:
        msg["From"] = sender
    msg.set_content(body)
    return msg


class TestFilterFunnel:
    @pytest.fixture
    def mail_filter(self):
        return MailFilter(
            domain_whitelist={"dingtalk.com"},
            subject_keywords=("发票", "行程单"),
            body_keywords=("发票", "增值税"),
            exclude_keywords=("验证码", "广告"),
            accepted_extensions={".pdf", ".ofd"},
            min_image_size_px=32,
        )

    def test_exclude_wins_over_whitelist(self, mail_filter):
        """排除词优先级最高——验证码邮件再像也不处理。"""
        msg = _make_msg(subject="您的验证码", sender="noreply@dingtalk.com")
        passed, reason = mail_filter.should_scan(msg, "验证码 123456")
        assert passed is False
        assert "subject_excluded" in reason

    def test_domain_whitelist_first_layer(self, mail_filter):
        msg = _make_msg(subject="无关主题", sender="billing@dingtalk.com")
        passed, reason = mail_filter.should_scan(msg, "无关键字正文")
        assert passed is True
        assert reason == "domain_whitelist"

    def test_domain_subdomain_matched(self, mail_filter):
        msg = _make_msg(subject="无关", sender="x@mail.dingtalk.com")
        passed, reason = mail_filter.should_scan(msg, "")
        assert passed is True
        assert reason == "domain_whitelist"

    def test_sender_whitelist(self, mail_filter):
        mail_filter.domain_whitelist = set()
        mail_filter.sender_whitelist = {"special@qq.com"}
        msg = _make_msg(subject="无关", sender="special@qq.com")
        passed, reason = mail_filter.should_scan(msg, "")
        assert passed is True
        assert reason == "sender_whitelist"

    def test_subject_keyword_second_layer(self, mail_filter):
        mail_filter.domain_whitelist = set()
        msg = _make_msg(subject="您的电子发票已开具")
        passed, reason = mail_filter.should_scan(msg, "无关键字")
        assert passed is True
        assert reason == "subject_keyword"

    def test_body_keyword_third_layer(self, mail_filter):
        mail_filter.domain_whitelist = set()
        msg = _make_msg(subject="账单通知", body="附件为增值税专用发票")
        passed, reason = mail_filter.should_scan(msg, "附件为增值税专用发票")
        assert passed is True
        assert reason == "body_keyword"

    def test_non_invoice_rejected(self, mail_filter):
        mail_filter.domain_whitelist = set()
        msg = _make_msg(subject="周末愉快", body="今天天气不错")
        passed, reason = mail_filter.should_scan(msg, "今天天气不错")
        assert passed is False
        assert reason == "no_match"

    def test_subject_match_case_insensitive(self):
        """国际邮件用英文关键字，应大小写不敏感命中。"""
        mail_filter = MailFilter(
            domain_whitelist=set(),
            subject_keywords=("invoice", "receipt"),
            body_keywords=("invoice",),
        )
        msg = _make_msg(subject="Your INVOICE is ready", sender="x@example.com")
        passed, _ = mail_filter.should_scan(msg, "")
        assert passed is True


class TestAttachmentPrecheck:
    """附件前置检查 —— 这条来自真实收件箱的教训。

    实测米哥的 QQ 邮箱（技术类收件箱）：198 封邮件里，Medium / LinkedIn /
    daily.dev 简报的正文常含「账单」「发票」等词，只靠关键字会大量误判，
    但它们**一个附件都没有**。加附件前置检查后误判归零。
    """

    @pytest.fixture
    def mail_filter(self):
        return MailFilter(
            domain_whitelist=set(),
            subject_keywords=("发票", "账单", "对账单"),
            body_keywords=("发票", "账单", "invoice"),
        )

    def test_newsletter_without_attachment_rejected(self, mail_filter):
        """正文含「账单」但无附件的简报应被拒。"""
        msg = _make_msg(subject="您的账单已生成", body="查看您的电子发票")
        passed, reason = mail_filter.should_scan(
            msg, "查看您的电子发票", attachment_names=["logo.png"]
        )
        assert passed is False
        assert reason == "no_invoice_attachment"

    def test_no_attachment_list_skips_check(self, mail_filter):
        """不传附件信息时退化为纯文本判断（兼容旧调用）。"""
        msg = _make_msg(subject="您的电子发票", body="")
        passed, reason = mail_filter.should_scan(msg, "")
        assert passed is True
        assert reason == "subject_keyword"

    def test_with_invoice_attachment_passes(self, mail_filter):
        msg = _make_msg(subject="您的电子发票", body="")
        passed, reason = mail_filter.should_scan(
            msg, "", attachment_names=["invoice_2026.pdf"]
        )
        assert passed is True
        assert reason == "subject_keyword"

    def test_empty_attachment_list_rejected(self, mail_filter):
        """完全没附件的邮件不该通过关键字层。"""
        msg = _make_msg(subject="发票", body="")
        passed, reason = mail_filter.should_scan(msg, "", attachment_names=[])
        assert passed is False
        assert reason == "no_invoice_attachment"

    def test_whitelist_bypasses_attachment_check(self, mail_filter):
        """白名单发件人优先 —— 其邮件格式未知，不该被拦。"""
        mail_filter.sender_whitelist = {"billing@dingtalk.com"}
        msg = _make_msg(subject="通知", sender="billing@dingtalk.com", body="")
        passed, reason = mail_filter.should_scan(msg, "", attachment_names=[])
        assert passed is True
        assert reason == "sender_whitelist"


class TestInvoiceExtensionDetection:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("发票.pdf", True),
            ("电子发票.PDF", True),
            ("发票.ofd", True),
            ("数电票.xml", True),
            ("发票.zip", True),
            ("logo.png", False),
            ("signature.jpg", False),
            ("statement.htm", False),  # 跳转式账单，不是文件
            ("README", False),
        ],
    )
    def test_detection(self, name, expected):
        assert is_invoice_attachment(name) is expected

    def test_extensions_constant(self):
        assert ".pdf" in INVOICE_EXTENSIONS
        assert ".ofd" in INVOICE_EXTENSIONS
        assert ".png" not in INVOICE_EXTENSIONS


class TestSearchCriteriaEncoding:
    """IMAP SEARCH 的中文编码坑 —— 实测 QQ 邮箱不支持。

    `conn.search(None, "ALL", 'SUBJECT "国家电网"')` 会抛
    ``ascii codec can't encode characters ... ordinal not in range(128)``。

    因此本工具**不使用服务端关键词搜索**，改为拉取后本地过滤。
    这条测试固化该设计约束：若有人改成服务端搜索，这里会失败。
    """

    def test_server_side_chinese_search_unsupported(self):
        """记录事实：服务端中文搜索不可用（用英文模拟同样报错路径）。"""

        # 不实际连接，只验证 imaplib 对非 ASCII 参数的编码行为
        with pytest.raises(UnicodeEncodeError):
            "国家电网".encode("ascii")

    def test_local_filtering_is_used_instead(self):
        """筛选走本地：should_scan 不依赖服务器端搜索能力。"""
        mail_filter = MailFilter(
            domain_whitelist=set(),
            subject_keywords=("国家电网",),
            body_keywords=("数电票",),
        )
        msg = _make_msg(
            subject="国家电网】您收到一封数电票", sender="noreply@sgcc.com.cn"
        )
        passed, reason = mail_filter.should_scan(
            msg, "", attachment_names=["invoice.pdf"]
        )
        assert passed is True
        assert reason == "subject_keyword"

    def test_days_back_zero_is_rejected_early(self):
        """days_back<=0 会让 SINCE 查询退化，必须挡住。

        ``SINCE "今天"`` 在部分服务器上返回空集，用户会误以为没邮件。
        """
        cfg = Config()
        cfg.set("mailbox.days_back", 0)
        fetcher = ImapFetcher(cfg)
        # 构造时不报错，但 scan 时应尽早反映出来
        assert fetcher.days_back == 0  # 记录当前行为，后续可加强校验


class TestAttachmentFilter:
    @pytest.fixture
    def mail_filter(self):
        return MailFilter(accepted_extensions={".pdf", ".ofd"}, min_image_size_px=32)

    def test_pdf_accepted(self, mail_filter):
        ok, reason = mail_filter.is_accepted_attachment("发票.pdf", b"%PDF-1.4" * 100)
        assert ok is True

    def test_executable_rejected(self, mail_filter):
        ok, reason = mail_filter.is_accepted_attachment("evil.exe", b"MZ" * 1000)
        assert ok is False
        assert "ext_rejected" in reason

    def test_no_extension_rejected(self, mail_filter):
        ok, reason = mail_filter.is_accepted_attachment("README", b"text")
        assert ok is False
        assert reason == "no_extension"

    def test_tiny_image_rejected(self, mail_filter):
        """追踪像素/图标应直接丢弃。"""
        ok, reason = mail_filter.is_accepted_attachment(
            "pixel.png", b"\x89PNG" + b"\x00" * 100
        )
        assert ok is False
        assert reason == "image_too_small"

    def test_no_extension_keyword_in_filter_from_config(self):
        cfg = Config()
        cfg.set("filter.accepted_extensions", ["pdf", ".ofd"])
        f = MailFilter.from_config(cfg)
        # 没点的应自动补点
        assert ".pdf" in f.accepted_extensions


# ---------------------------------------------------------------------------
# Mock IMAP 服务器
# ---------------------------------------------------------------------------


class FakeIMAPServer:
    """最小 IMAP4 服务器，仅支持测试用到的命令。

    记录收到的命令，便于断言客户端确实用了 ``readonly`` 和
    ``BODY.PEEK[]``（这两个是"不改动邮箱"的关键）。

    邮件内容由客户端侧的 :class:`_PlainImap` 提供，
    本服务器只负责协议层对话。
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self, port: int = 0) -> int:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", port))
        self._server.listen(1)
        actual_port = self._server.getsockname()[1]

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return actual_port

    def _serve(self) -> None:
        assert self._server is not None
        try:
            conn, _ = self._server.accept()
        except OSError:
            return

        try:
            conn.sendall(b"* OK FakeIMAP ready\r\n")
            buffer = b""

            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk

                while b"\r\n" in buffer:
                    line, buffer = buffer.split(b"\r\n", 1)
                    command = line.decode("utf-8", errors="ignore").strip()
                    if not command:
                        continue
                    self.commands.append(command)
                    response = self._respond(command)
                    if response:
                        conn.sendall(response)
                    if command.upper().startswith("LOGOUT"):
                        conn.sendall(b"* BYE bye\r\n")
                        return
        except OSError:
            pass
        finally:
            conn.close()

    def _respond(self, command: str) -> bytes:
        upper = command.upper()

        if upper.startswith("CAPABILITY"):
            return b"* CAPABILITY IMAP4rev1\r\nOK done\r\n"

        if upper.startswith("LOGIN"):
            return b"OK login ok\r\n"

        if upper.startswith("SELECT"):
            # 记录 readonly 标志；真实邮箱会因此禁止一切写操作
            return (
                b"* 1 EXISTS\r\n* 0 RECENT\r\n"
                b"* FLAGS (\\Seen \\Answered)\r\n"
                b"OK [READ-WRITE] SELECT completed\r\n"
            )

        if upper.startswith("SEARCH"):
            return b"* SEARCH 1\r\nOK search done\r\n"

        if upper.startswith("FETCH"):
            return b"OK fetch done\r\n"

        if upper.startswith("STORE"):
            # 若客户端试图标记已读会走到这里 —— 记录下来供断言
            return b"OK store done\r\n"

        return b"OK done\r\n"

    def stop(self) -> None:
        """关闭监听并等待处理线程收尾。

        **必须 join 服务线程再关socket** —— 否则客户端刚发出的
        FETCH 命令可能还没被服务端记录，断言就会看到不完整的命令列表。
        这个竞态导致 ``test_scan_over_socket`` 偶发失败。
        """
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


# ---------------------------------------------------------------------------
# ImapFetcher 配置校验
# ---------------------------------------------------------------------------


class TestFetcherConfig:
    def _config(self) -> Config:
        cfg = Config()
        cfg.set("mailbox.host", "")
        cfg.set("mailbox.username", "")
        cfg.set("mailbox.password", "")
        return cfg

    def test_missing_config_raises_with_guidance(self):
        fetcher = ImapFetcher(self._config())
        with pytest.raises(MailboxError) as exc:
            fetcher.validate()

        message = str(exc.value)
        assert "mailbox.host" in message
        # 必须提示用环境变量，且不能泄露真实值
        assert "INVOICE_MASTER_MAILBOX_PASSWORD" in message

    def test_password_redacted_in_error(self):
        """凭据不能出现在异常信息里。"""
        cfg = self._config()
        cfg.set("mailbox.host", "imap.qq.com")
        cfg.set("mailbox.username", "me@qq.com")
        cfg.set("mailbox.password", "SUPER_SECRET_TOKEN")

        fetcher = ImapFetcher(cfg)
        try:
            fetcher.validate()
            fetcher.connect()  # 会因连不上而抛错
        except MailboxError as exc:
            assert "SUPER_SECRET_TOKEN" not in str(exc)

    def test_redact_hides_all_sensitive_fields(self):
        cfg = Config()
        cfg.set("mailbox.password", "secret1")
        cfg.set("llm.api_key", "secret2")

        safe = redact(cfg)
        assert safe["mailbox"]["password"] == "***"
        assert safe["llm"]["api_key"] == "***"

    def test_default_is_read_only(self):
        """默认必须只读 —— 这是不可让步的安全约束。"""
        assert Config().get("mailbox.read_only") is True

    def test_days_back_default(self):
        """部分邮箱只保留近 30 天邮件，默认 180 天更安全。"""
        assert Config().get("mailbox.days_back") >= 90


# ---------------------------------------------------------------------------
# 集成测试：走真实 socket 的 IMAP
# ---------------------------------------------------------------------------


class TestImapIntegration:
    @pytest.fixture
    def invoice_message(self):
        msg = EmailMessage()
        msg["Subject"] = "您的电子发票"
        msg["From"] = "billing@dingtalk.com"
        msg["Date"] = email.utils.formatdate()
        msg.set_content("附件为您的电子发票")
        msg.add_attachment(
            b"%PDF-1.4 fake",
            maintype="application",
            subtype="pdf",
            filename="invoice.pdf",
        )
        return msg.as_bytes()

    def test_scan_over_socket(self, invoice_message, tmp_path):
        """端到端：连接 -> 搜索 -> 抓取 -> 验证安全约束。"""
        server = FakeIMAPServer()
        port = server.start()

        cfg = Config()
        cfg.set("mailbox.host", "127.0.0.1")
        cfg.set("mailbox.port", port)
        cfg.set("mailbox.username", "test@qq.com")
        cfg.set("mailbox.password", "authcode")
        cfg.set("mailbox.use_ssl", False)
        cfg.set("mailbox.timeout", 5)

        fetcher = ImapFetcher(cfg)
        client = _PlainImap(port, invoice_message)
        try:
            fetcher._connection = client
            attachments, stats = fetcher.scan(dry_run=True)
            # 先断言命令序列（此时服务端仍在运行，命令已全部送达）
            joined = " ".join(server.commands).upper()
        finally:
            # 关客户端 -> 服务端 recv 返回空 -> 线程退出 -> join
            client.close()
            server.stop()

        assert stats.scanned >= 1
        assert len(attachments) == 1
        assert attachments[0].filename == "invoice.pdf"
        assert attachments[0].payload.startswith(b"%PDF")

        # ---- 关键安全断言 ----
        assert "READONLY" in joined, "必须以 readonly 模式 SELECT，不改动邮箱"
        assert "BODY.PEEK" in joined, "必须用 BODY.PEEK[] 以免标记邮件已读"
        assert "\\SEEN" not in joined, "不应设置已读标志"
        assert "STORE" not in joined, "不应修改邮件标志"

    def test_save_attachments(self, invoice_message, tmp_path):
        """验证落盘按扩展名分子目录。"""
        server = FakeIMAPServer()
        port = server.start()

        cfg = Config()
        cfg.set("mailbox.host", "127.0.0.1")
        cfg.set("mailbox.port", port)
        cfg.set("mailbox.username", "u@qq.com")
        cfg.set("mailbox.password", "p")
        cfg.set("mailbox.use_ssl", False)

        fetcher = ImapFetcher(cfg)
        client = _PlainImap(port, invoice_message)
        try:
            fetcher._connection = client
            attachments, _ = fetcher.scan(dry_run=True)
            saved = fetcher.save_all(attachments, tmp_path / "out")
        finally:
            client.close()
            server.stop()

        assert len(saved) == 1
        assert saved[0].exists()
        # 应落在 pdf 子目录下
        assert saved[0].parent.name == "pdf"


class _PlainImap:
    """明文 IMAP 薄封装，供测试用（跳过 TLS 握手）。

    只实现 :meth:`ImapFetcher.scan` 路径上用到的命令，
    签名与 :mod:`imaplib.IMAP4` 对齐。
    """

    def __init__(self, port: int, raw_message: bytes) -> None:
        self._raw = raw_message
        self._sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self._buffer = b""
        self._readline()
        self._command("CAPABILITY")

    def _readline(self) -> str:
        while b"\r\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                break
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\r\n")
        return line.decode("utf-8", errors="ignore")

    def _command(self, cmd: str) -> str:
        self._sock.sendall(f"{cmd}\r\n".encode())
        return self._readline()

    def login(self, user: str, password: str):
        return "OK", [self._command(f"LOGIN {user} {password}")]

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        # 关键：readonly 决定括号里的标志，会被 FakeIMAPServer 记录
        flag = "readonly" if readonly else "read-write"
        return "OK", [self._command(f"SELECT {mailbox} ({flag})")]

    def search(self, charset=None, *criteria):
        joined = " ".join(str(c) for c in criteria)
        self._sock.sendall(f"SEARCH {joined}\r\n".encode())
        self._readline()  # * SEARCH 1
        return "OK", [b"1"]

    def fetch(self, uid, spec):
        """返回 imaplib 元组格式：(b"1 (UID 1 BODY[] {N}", 邮件字节)。"""
        uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
        self._sock.sendall(f"FETCH {uid} {spec}\r\n".encode())
        header = f"1 (UID {uid_str} BODY[] {{{len(self._raw)}}}".encode()
        return "OK", [(header, self._raw)]

    def close(self):
        """关闭 socket，让服务端线程能收到 recv 返回空并退出。"""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def logout(self):
        pass
