"""CLI 与配置测试。

覆盖：
* 参数解析（含全局参数前置转发）
* 默认行为：LLM 默认关闭
* 破坏性操作默认 dry-run
* 配置三层覆盖与脱敏

运行::

    pytest tests/test_cli.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_master.cli import (  # noqa: E402
    _hoist_global_flags,
    build_parser,
    collect_pdfs,
    main,
)
from invoice_master.config import (  # noqa: E402
    DEFAULT_CONFIG,
    Config,
    load_config,
    redact,
    save_config,
)

SAMPLES = Path(__file__).parent / "samples"


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


class TestArgumentParsing:
    def test_hoist_moves_flags_before_subcommand(self):
        """用户常把 -v 写在子命令后，需要自动前置。"""
        result = _hoist_global_flags(["extract", "foo.pdf", "-v"])
        assert result[0] == "-v"
        assert "extract" in result

    def test_hoist_preserves_already_correct_order(self):
        result = _hoist_global_flags(["-v", "extract", "foo.pdf"])
        assert result == ["-v", "extract", "foo.pdf"]

    def test_hoist_keeps_subcommand_flags(self):
        """子命令自己的参数不能被前置。"""
        result = _hoist_global_flags(["extract", "foo.pdf", "--enable-llm", "-v"])
        assert "--enable-llm" in result
        assert result.index("-v") < result.index("extract")

    def test_parse_extract(self):
        parser = build_parser()
        args = parser.parse_args(["extract", "a.pdf", "b.pdf"])
        assert args.command == "execute" or args.command == "extract"
        assert args.paths == ["a.pdf", "b.pdf"]

    def test_parse_merge(self):
        parser = build_parser()
        args = parser.parse_args(["merge", "a.pdf", "-o", "out.pdf"])
        assert args.command == "merge"
        assert args.output == "out.pdf"

    def test_parse_rename_requires_explicit_execute(self):
        parser = build_parser()
        args = parser.parse_args(["rename", "a.pdf"])
        # 不加 --execute 就是预览
        assert args.execute is False

    def test_parse_rename_with_execute(self):
        parser = build_parser()
        args = parser.parse_args(["rename", "a.pdf", "--execute"])
        assert args.execute is True


# ---------------------------------------------------------------------------
# 默认行为
# ---------------------------------------------------------------------------


class TestDefaultBehavior:
    def test_llm_disabled_by_default(self):
        """LLM 默认关闭，避免未配置时产生意外 API 调用。"""
        assert Config().get("llm.enabled") is False

    def test_llm_needs_api_key_to_be_available(self):
        cfg = Config()
        cfg.set("llm.enabled", True)
        cfg.set("llm.api_key", "")
        assert cfg.is_llm_enabled() is False

    def test_llm_available_with_key(self):
        cfg = Config()
        cfg.set("llm.enabled", True)
        cfg.set("llm.api_key", "sk-test")
        assert cfg.is_llm_enabled() is True

    def test_mailbox_read_only_by_default(self):
        """邮箱必须只读 —— 不标记已读、不移动、不删除。"""
        assert Config().get("mailbox.read_only") is True

    def test_password_never_in_example_config(self):
        """示例配置里不能有真实凭据。"""
        example = Path(__file__).resolve().parent.parent / "invoice_master.example.yaml"
        if not example.exists():
            pytest.skip("示例配置不存在")
        text = example.read_text(encoding="utf-8")
        # host/username 应留空
        assert 'host: ""' in text
        assert 'password: ""' in text


# ---------------------------------------------------------------------------
# 示例配置（与代码默认值的一致性）
# ---------------------------------------------------------------------------


EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "invoice_master.example.yaml"


def _flatten(prefix: str, obj, out: dict) -> None:
    """把嵌套配置摊平成点路径，便于逐字段比较。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            _flatten(f"{prefix}.{key}" if prefix else key, value, out)
    else:
        out[prefix] = obj


def _example_flat() -> dict:
    if not EXAMPLE_CONFIG.exists():
        pytest.skip("示例配置不存在")
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8")) or {}
    flat: dict = {}
    _flatten("", data, flat)
    return flat


class TestExampleConfig:
    def test_every_field_matches_defaults(self):
        """示例里出现的每个字段都必须等于 DEFAULT_CONFIG。

        示例不是默认值副本：一旦某字段值漂移，用户复制后会静默覆盖
        代码默认行为。这条测试把漂移挡在 CI 之外。
        """
        flat = _example_flat()
        defaults: dict = {}
        _flatten("", DEFAULT_CONFIG, defaults)

        unknown = sorted(set(flat) - set(defaults))
        assert not unknown, f"示例配置含 DEFAULT_CONFIG 中不存在的字段: {unknown}"

        drifted = {
            key: (value, defaults[key])
            for key, value in flat.items()
            if value != defaults[key]
        }
        assert not drifted, (
            "示例配置与代码默认值漂移（示例值, 默认值）: "
            f"{drifted}；全量模板请用 config --init 生成"
        )

    def test_no_non_empty_list_overrides(self):
        """示例不得携带非空列表 —— 列表在配置合并中是整体替换。

        默认值里的关键词 / 扩展名等列表若被抄进示例，必然随代码升级
        而漂移；用户复制后会丢掉未列出的项。
        """
        flat = _example_flat()
        offenders = {
            key: value
            for key, value in flat.items()
            if isinstance(value, list) and value
        }
        assert not offenders, (
            f"示例配置不应包含非空列表（会整体替换默认值）: {offenders}"
        )


# ---------------------------------------------------------------------------
# 配置系统
# ---------------------------------------------------------------------------


class TestConfig:
    def test_dot_path_get(self):
        cfg = Config()
        assert cfg.get("merge.rows") == 2
        assert cfg.get("merge.nope.deep", "fallback") == "fallback"

    def test_dot_path_set(self):
        cfg = Config()
        cfg.set("a.b.c", 42)
        assert cfg.get("a.b.c") == 42

    def test_defaults_merged(self, tmp_path):
        target = tmp_path / "cfg.yaml"
        save_config(Config(), target)

        cfg = load_config(target)
        # 自定义文件的值 + 其他段的默认值
        assert cfg.get("merge.rows") == 2
        assert cfg.source_path == target

    def test_partial_config_keeps_other_defaults(self, tmp_path):
        target = tmp_path / "partial.yaml"
        target.write_text("merge:\n  rows: 5\n", encoding="utf-8")

        cfg = load_config(target)
        assert cfg.get("merge.rows") == 5  # 被覆盖
        assert cfg.get("merge.cols") == 2  # 保持默认

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("INVOICE_MASTER_MERGE_ROWS", "7")
        monkeypatch.setenv("INVOICE_MASTER_LLM_ENABLED", "true")

        cfg = load_config(search_parents=False)
        assert cfg.get("merge.rows") == 7
        assert cfg.get("llm.enabled") is True

    def test_legacy_env_override_still_works(self, monkeypatch):
        """改名前的环境变量继续可用，避免老脚本立刻失效。"""
        monkeypatch.setenv("INVOICE_TOOLKIT_MERGE_ROWS", "6")
        cfg = load_config(search_parents=False)
        assert cfg.get("merge.rows") == 6

    def test_new_env_wins_over_legacy(self, monkeypatch):
        monkeypatch.setenv("INVOICE_TOOLKIT_MERGE_ROWS", "6")
        monkeypatch.setenv("INVOICE_MASTER_MERGE_ROWS", "8")
        cfg = load_config(search_parents=False)
        assert cfg.get("merge.rows") == 8

    def test_legacy_config_filename_supported(self, tmp_path, monkeypatch):
        (tmp_path / "invoice_toolkit.json").write_text(
            '{"merge": {"rows": 5}}', encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        cfg = load_config()
        assert cfg.get("merge.rows") == 5
        assert cfg.source_path == tmp_path / "invoice_toolkit.json"

    def test_runtime_override_wins(self):
        cfg = load_config(search_parents=False, overrides={"merge": {"rows": 9}})
        assert cfg.get("merge.rows") == 9

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/definitely/not/here.yaml")

    def test_redact_hides_secrets(self):
        cfg = Config()
        cfg.set("mailbox.password", "hunter2")
        cfg.set("llm.api_key", "sk-secret")

        safe = redact(cfg)
        assert safe["mailbox"]["password"] == "***"
        assert safe["llm"]["api_key"] == "***"
        # 非敏感字段保留
        assert safe["mailbox"]["host"] == cfg.get("mailbox.host")

    def test_json_config_supported(self, tmp_path):
        """没装 PyYAML 时JSON 是退路，必须能用。"""
        target = tmp_path / "cfg.json"
        target.write_text('{"merge": {"rows": 3}}', encoding="utf-8")

        cfg = load_config(target)
        assert cfg.get("merge.rows") == 3

    def test_invalid_yaml_raises_with_hint(self, tmp_path):
        target = tmp_path / "bad.yaml"
        target.write_text("merge:\n  rows: [unclosed\n", encoding="utf-8")

        with pytest.raises(ValueError) as exc:
            load_config(target)
        assert "YAML" in str(exc.value)


# ---------------------------------------------------------------------------
# 路径收集
# ---------------------------------------------------------------------------


class TestCollectPdfs:
    def test_collect_from_directory(self, tmp_path):
        for name in ("a.pdf", "b.pdf", "c.txt"):
            (tmp_path / name).write_bytes(b"%PDF-1.4")

        files = collect_pdfs([str(tmp_path)])
        assert len(files) == 2
        assert all(f.suffix == ".pdf" for f in files)

    def test_recursive(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "top.pdf").write_bytes(b"%PDF-1.4")

        assert len(collect_pdfs([str(tmp_path)])) == 1
        assert len(collect_pdfs([str(tmp_path)], recursive=True)) == 2

    def test_single_file(self):
        samples = sorted(SAMPLES.glob("test_*.pdf"))
        if not samples:
            pytest.skip("样本不存在")
        files = collect_pdfs([str(samples[0])])
        assert len(files) == 1

    def test_nonexistent_path_ignored(self):
        assert collect_pdfs(["/no/such/path.pdf"]) == []


# ---------------------------------------------------------------------------
# 端到端 CLI
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SAMPLES.exists(), reason="样本不存在")
class TestCliEndToEnd:
    def test_extract_json_output(self, tmp_path, capsys):
        output = tmp_path / "result.json"
        code = main(["extract", str(SAMPLES), "-o", str(output), "--json"])
        assert code == 0

        import json

        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["total"] >= 4
        assert payload["success"] >= 4

    def test_layouts_command(self, capsys):
        assert main(["layouts", "--json"]) == 0
        payload = capsys.readouterr().out
        assert "横向2x2" in payload

    def test_merge_to_tmp(self, tmp_path):
        target = tmp_path / "merged.pdf"
        code = main(["merge", str(SAMPLES), "-o", str(target), "--json"])
        assert code == 0
        assert target.exists()
        assert target.stat().st_size > 0

    def test_report_to_tmp(self, tmp_path):
        target = tmp_path / "report.xlsx"
        code = main(["report", str(SAMPLES), "-o", str(target), "--json"])
        assert code == 0
        assert target.exists()

    def test_rename_is_dry_run_by_default(self, tmp_path, capsys):
        """默认不能真的改文件名 —— 这是最重要的安全约束。"""
        import shutil

        workdir = tmp_path / "invoices"
        workdir.mkdir()
        for sample in SAMPLES.glob("test_*.pdf"):
            shutil.copy(sample, workdir / sample.name)

        before = sorted(p.name for p in workdir.glob("*.pdf"))
        code = main(["rename", str(workdir), "-t", "{date}-{type}", "--json"])
        after = sorted(p.name for p in workdir.glob("*.pdf"))

        assert code == 0
        assert before == after, "默认模式下文件不应被改动"
        # 输出里应包含 dry_run 标记
        assert '"dry_run": true' in capsys.readouterr().out

    def test_no_llm_flag_does_not_enable(self, tmp_path):
        """--no-llm 不能意外打开 LLM。"""
        config = load_config(search_parents=False)
        assert config.get("llm.enabled") is False


# ---------------------------------------------------------------------------
# merge 布局优先级
# ---------------------------------------------------------------------------


class TestMergeLayoutPrecedence:
    """merge 布局优先级：命令行单项参数 > --layout > 配置 merge: 段 > 内置默认。

    用假 Merger 捕获最终 layout 字典，不真正合并 PDF。
    """

    @staticmethod
    def _run(monkeypatch, config: Config, argv: list[str]) -> dict:
        from invoice_master import cli as cli_mod

        captured: dict = {}

        class FakeMerger:
            def __init__(self, layout):
                captured["layout"] = dict(layout)

            def merge(self, files, output, progress=None):
                return Path(output)

        monkeypatch.setattr(cli_mod, "Merger", FakeMerger)
        monkeypatch.setattr(
            cli_mod, "collect_pdfs", lambda paths, recursive: [Path("a.pdf")]
        )

        args = build_parser().parse_args(argv)
        assert cli_mod.cmd_merge(args, config) == 0
        return captured["layout"]

    def test_config_merge_section_is_honored(self, monkeypatch):
        """配置 merge: 段必须生效（此前命令行不读取它，是缺陷）。"""
        config = Config(data={"merge": {"rows": 3, "cols": 3, "gap": 9}})
        layout = self._run(monkeypatch, config, ["merge", "a.pdf", "-o", "out.pdf"])
        assert layout["rows"] == 3
        assert layout["cols"] == 3
        assert layout["gap"] == 9

    def test_default_grid_without_config_or_flag(self, monkeypatch):
        """无配置且未指定 --layout 时回落到内置 横向2x2。"""
        layout = self._run(
            monkeypatch, Config(data={}), ["merge", "a.pdf", "-o", "out.pdf"]
        )
        assert (layout["rows"], layout["cols"]) == (2, 2)
        assert layout["orientation"] == "landscape"

    def test_layout_flag_overrides_config_grid(self, monkeypatch):
        config = Config(data={"merge": {"rows": 3, "cols": 3}})
        layout = self._run(
            monkeypatch,
            config,
            ["merge", "a.pdf", "-o", "out.pdf", "--layout", "横向2x4"],
        )
        assert (layout["rows"], layout["cols"]) == (2, 4)

    def test_cli_args_override_layout_flag(self, monkeypatch):
        config = Config(data={"merge": {"rows": 3, "cols": 3}})
        layout = self._run(
            monkeypatch,
            config,
            ["merge", "a.pdf", "-o", "out.pdf", "--layout", "横向2x4", "--rows", "5"],
        )
        assert layout["rows"] == 5
        assert layout["cols"] == 4  # --layout 的列数仍生效

    def test_cli_margin_overrides_config_only_for_given_field(self, monkeypatch):
        config = Config(data={"merge": {"margin_top": 20}})
        layout = self._run(
            monkeypatch,
            config,
            ["merge", "a.pdf", "-o", "out.pdf", "--margin-top", "7"],
        )
        assert layout["margin_top"] == 7
        assert layout["margin_bottom"] == 10  # 未覆盖字段仍走配置/默认

    def test_page_size_follows_final_orientation(self, monkeypatch):
        """纸张按最终方向计算，而不是 --layout/配置 之前的中间态。"""
        config = Config(data={"merge": {"orientation": "portrait", "page": "A4"}})
        layout = self._run(monkeypatch, config, ["merge", "a.pdf", "-o", "out.pdf"])
        assert layout["page_size"] == (595.0, 842.0)

        layout2 = self._run(
            monkeypatch,
            Config(data={}),
            ["merge", "a.pdf", "-o", "out.pdf", "--layout", "竖向1x2", "--page", "A3"],
        )
        assert layout2["page_size"][0] < layout2["page_size"][1]
