"""端對端測試：以子行程執行真正的 CLI，再用 Markdown 解析器檢查輸出結構。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest
from markdown_it import MarkdownIt

SUPPORTED_FIXTURES = [
    "testOneNote.one",
    "testOneNote1.one",
    "testOneNote3.one",
    "testOneNote2016.one",
    "testOneNoteEmbeddedWordDoc.one",
    "test-tika-4303-Chinese-notes.one",
]

markdown_parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "onenote_export.cli", *map(str, args)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


@pytest.fixture
def notebook_dir(tmp_path, fixtures_dir) -> Path:
    """組出一個像 OneNote 備份資料夾的結構：筆記本/節.one (日期).one。"""
    notebook = tmp_path / "input" / "測試筆記本"
    notebook.mkdir(parents=True)
    for name in SUPPORTED_FIXTURES:
        stem = name.removesuffix(".one")
        (notebook / f"{stem}.one (2026-1-1).one").write_bytes((fixtures_dir / name).read_bytes())
    # 同一節較舊的備份：預設應被略過
    (notebook / "testOneNote2016.one (2025-1-1).one").write_bytes((fixtures_dir / "testOneNote3.one").read_bytes())
    recycle = notebook / "OneNote_RecycleBin"
    recycle.mkdir()
    (recycle / "已刪除.one (2026-1-1).one").write_bytes((fixtures_dir / "testOneNote2016.one").read_bytes())
    return tmp_path / "input"


def render_html(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    _, _, content = text.partition("\n---\n")  # 去掉 front matter
    return markdown_parser.render(content)


def test_markdown_export_end_to_end(notebook_dir, tmp_path):
    output = tmp_path / "out"
    result = run_cli(notebook_dir, "-o", output)
    assert result.returncode == 0, result.stderr
    assert "節：6／6" in result.stdout
    assert "圖片：匯出 39" in result.stdout

    section_dir = output / "測試筆記本" / "testOneNote1"
    pages = sorted(section_dir.glob("*.md"))
    assert [page.name for page in pages] == [
        "001 OneNote one place for all of your notes.md",
        "002 OneNote Basics.md",
        "index.md",
    ]

    html = render_html(pages[0])
    assert "<h1>OneNote: one place for all of your notes</h1>" in html
    assert "<table>" in html
    assert '<a href="http://o15.officeredir.microsoft.com/r/rlidOneNoteGuideVideo15?clid=1033">' in html
    assert "HYPERLINK" not in html

    # 每個圖片連結都指向實際存在的檔案

    sources = re.findall(r'<img src="([^"]+)"', html)
    assert sources
    for source in sources:
        assert (section_dir / unquote(source)).is_file(), source

    # 最新備份優先、回收筒預設不匯出
    assert "So good" in (output / "測試筆記本" / "testOneNote2016" / "001 So good.md").read_text(encoding="utf-8")
    assert not (output / "測試筆記本" / "OneNote_RecycleBin").exists()

    # 附件原樣輸出
    attachment = output / "測試筆記本" / "testOneNoteEmbeddedWordDoc" / "assets"
    (docx,) = attachment.rglob("*.docx")
    assert docx.read_bytes().startswith(b"PK\x03\x04")

    # 索引頁連結到每個節與頁面
    root_html = markdown_parser.render((output / "index.md").read_text(encoding="utf-8"))
    for href in re.findall(r'<a href="([^"]+)"', root_html):
        assert (output / unquote(href)).is_file(), href
    section_html = markdown_parser.render((section_dir / "index.md").read_text(encoding="utf-8"))
    for href in re.findall(r'<a href="([^"]+)"', section_html):
        assert (section_dir / unquote(href)).is_file(), href


def test_front_matter_metadata(notebook_dir, tmp_path):
    output = tmp_path / "out"
    assert run_cli(notebook_dir, "-o", output).returncode == 0
    page = output / "測試筆記本" / "testOneNote2016" / "001 So good.md"
    header = page.read_text(encoding="utf-8").split("---\n")[1]
    fields = {line.split(": ", 1)[0]: json.loads(line.split(": ", 1)[1]) for line in header.strip().splitlines()}
    assert fields["title"] == "So good"
    assert fields["notebook"] == "測試筆記本"
    assert fields["author"] == "nicholas dipiazza"
    assert fields["created"].startswith("2019-12-")


def test_all_versions_and_recycle_bin_flags(notebook_dir, tmp_path):
    output = tmp_path / "out"
    result = run_cli(notebook_dir, "-o", output, "--all-versions", "--include-recycle-bin", "-q")
    assert result.returncode == 0, result.stderr
    section_dirs = {path.name for path in (output / "測試筆記本").iterdir()}
    assert {"testOneNote2016 (2026-01-01)", "testOneNote2016 (2025-01-01)", "OneNote_RecycleBin"} <= section_dirs


def test_json_export(notebook_dir, tmp_path):
    output = tmp_path / "out"
    assert run_cli(notebook_dir, "-o", output, "--format", "json", "-q").returncode == 0
    document = json.loads((output / "測試筆記本" / "testOneNote1" / "section.json").read_text(encoding="utf-8"))
    assert document["section"] == "testOneNote1"
    assert document["backup_date"] == "2026-01-01"
    assert [page["title"] for page in document["pages"]] == [
        "OneNote: one place for all of your notes",
        "OneNote Basics",
    ]
    image_paths = [element["path"] for element in document["pages"][0]["elements"] if element["type"] == "image"]
    assert image_paths and all((output / "測試筆記本" / "testOneNote1" / path).is_file() for path in image_paths)


def test_list_mode_writes_nothing(notebook_dir, tmp_path):
    output = tmp_path / "out"
    result = run_cli(notebook_dir, "--list", "-o", output)
    assert result.returncode == 0
    assert "測試筆記本/testOneNote1（2026-01-01）：2 頁" in result.stdout
    assert "  - OneNote Basics" in result.stdout
    assert not output.exists()


def test_refuses_non_empty_output_without_force(notebook_dir, tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep.txt").write_text("使用者的檔案")
    result = run_cli(notebook_dir, "-o", output)
    assert result.returncode == 2
    assert "--force" in result.stderr
    assert run_cli(notebook_dir, "-o", output, "--force", "-q").returncode == 0
    assert (output / "keep.txt").read_text() == "使用者的檔案"


def test_partial_failure_exit_code(tmp_path, fixtures_dir):
    folder = tmp_path / "input" / "混合"
    folder.mkdir(parents=True)
    (folder / "好.one").write_bytes((fixtures_dir / "testOneNote2016.one").read_bytes())
    (folder / "雲端格式.one").write_bytes((fixtures_dir / "testOneNoteFromOffice365.one").read_bytes())
    (folder / "壞掉.one").write_bytes(b"not a onenote file")

    result = run_cli(tmp_path / "input", "-o", tmp_path / "out")
    assert result.returncode == 1
    assert "節：1／3" in result.stdout
    assert "FSSHTTPB" in result.stdout
    assert (tmp_path / "out" / "混合" / "好" / "001 So good.md").is_file()


def test_missing_input_is_a_usage_error(tmp_path):
    result = run_cli(tmp_path / "不存在", "-o", tmp_path / "out")
    assert result.returncode == 2
    assert "找不到" in result.stderr


def test_version_flag():
    result = run_cli("--version")
    assert result.returncode == 0
    assert "onenote-export" in result.stdout
