import importlib.util
import io
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_copy.py"
spec = importlib.util.spec_from_file_location("check_public_copy", SCRIPT)
check_public_copy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_public_copy)


@pytest.mark.parametrize(
    "bad",
    [
        "Fetch data \u2014 fast.",
        "Pages 1\u20132.",
        "the \u201clive\u201d connectors",
        "it\u2019s done",
        "data -- then parse",
        "data--then parse",
        "copied here --\n  this connector",
        "copied here\n-- this connector",
        "Fetch data &mdash; fast.",
        "the &ldquo;live&rdquo; connectors",
        "see t_0a1b2c3d for details",
        "ruling d_deadbeef",
        "README_OK: keep",
        "set REFCAL_EXAMPLE_URL",
    ],
)
def test_flags_each_rule(bad):
    # Control: every rule fires on a known-bad line, so a clean run over the
    # real files means clean, not a checker that matches nothing.
    assert check_public_copy.find_problems(bad)


@pytest.mark.parametrize(
    "good",
    [
        "pip install --upgrade spectraccess",
        "| Source | Status |\n| --- | --- |",
        "---\ntitle: front matter\n---",
        "Sentinel-2 top-of-atmosphere reflectance",
        "don't use \"curly\" quotes",
        "the data_access helper",
    ],
)
def test_allows_ordinary_text(good):
    assert check_public_copy.find_problems(good) == []


def test_reports_line_and_column():
    problems = check_public_copy.find_problems("clean line\nsecond \u2014 line")
    assert [(line, column) for line, column, _, _ in problems] == [(2, 8)]


def test_stdin_mode_fails_on_bad_text(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("docs: tidy \u2014 wording\n"))
    assert check_public_copy.main(["--stdin", "commits"]) == 1
    assert "commits:1:" in capsys.readouterr().out


def test_missing_public_copy_file_fails(tmp_path):
    assert check_public_copy.main([str(tmp_path / "OVERVIEW.md")]) == 1


def test_repository_public_copy_is_clean(capsys):
    files = check_public_copy.public_copy_files()
    assert any(path.name == "DATA_TERMS.md" for path in files)
    assert check_public_copy.main([]) == 0, capsys.readouterr().out
