"""exporter.py：找檔、版本挑選、JSON 轉換。"""

from datetime import date
from pathlib import Path

from onenote_export.exporter import (
    ExportOptions,
    SourceFile,
    default_backup_dir,
    discover,
    page_to_json,
    select_sources,
)
from onenote_export.model import Image, Outline, OutlineItem, Page, Paragraph, TextRun
from onenote_export.naming import AssetAllocator


def source(section: str, day: int | None, notebook: str = "筆記本", recycle: bool = False) -> SourceFile:
    backup = date(2026, 1, day) if day else None
    return SourceFile(Path(f"/x/{section}-{day}.one"), notebook, section, backup, recycle)


def test_discover_walks_directories_and_reads_backup_names(tmp_path):
    (tmp_path / "筆記本" / "OneNote_RecycleBin").mkdir(parents=True)
    (tmp_path / "筆記本" / "備忘.one (2026-9-27).one").write_bytes(b"")
    (tmp_path / "筆記本" / "OneNote_RecycleBin" / "舊.one (2023-9-24).one").write_bytes(b"")
    (tmp_path / "筆記本" / "._備忘.one").write_bytes(b"")  # macOS 的 AppleDouble 檔
    (tmp_path / "筆記本" / "readme.txt").write_text("x")

    found = {(s.notebook, s.section, s.backup_date, s.in_recycle_bin) for s in discover([tmp_path])}
    assert found == {
        ("筆記本", "備忘", date(2026, 9, 27), False),
        ("筆記本/OneNote_RecycleBin", "舊", date(2023, 9, 24), True),
    }


def test_discover_accepts_single_files_and_skips_missing_paths(tmp_path):
    file = tmp_path / "我的筆記本" / "單一.one"
    file.parent.mkdir()
    file.write_bytes(b"")
    (single,) = discover([file, tmp_path / "不存在"])
    assert (single.notebook, single.section, single.backup_date) == ("我的筆記本", "單一", None)


def test_select_sources_keeps_latest_backup_per_section():
    sources = [source("備忘", 3), source("備忘", 20), source("備忘", 8), source("美食", None)]
    selected = select_sources(sources, ExportOptions(output=Path("out")))
    assert [(s.section, s.backup_date) for s in selected] == [("備忘", date(2026, 1, 20)), ("美食", None)]


def test_select_sources_all_versions_and_recycle_bin():
    sources = [source("備忘", 3), source("備忘", 20), source("已刪除", 1, recycle=True)]
    default = select_sources(sources, ExportOptions(output=Path("out")))
    assert [s.section for s in default] == ["備忘"]

    everything = select_sources(sources, ExportOptions(output=Path("out"), all_versions=True, include_recycle_bin=True))
    assert len(everything) == 3


def test_same_section_name_in_different_notebooks_is_kept_separately():
    sources = [source("備忘", 3, notebook="甲"), source("備忘", 5, notebook="乙")]
    assert len(select_sources(sources, ExportOptions(output=Path("out")))) == 2


def test_page_to_json_replaces_binary_data_with_asset_paths():
    page = Page(
        title="頁",
        elements=(
            Outline((OutlineItem((Paragraph((TextRun("文字", bold=True),)),)),)),
            Image(b"\x89PNG", ".png", filename="a.png"),
            Image(None, ".jpeg", filename="遺失.jpeg"),
        ),
    )
    allocator = AssetAllocator("assets/001 頁")
    result = page_to_json(page, allocator)

    outline, image, missing = result["elements"]
    assert result["type"] == "page" and result["title"] == "頁"
    assert outline["items"][0]["content"][0]["runs"][0] == {
        "type": "textrun",
        "text": "文字",
        "bold": True,
        "italic": False,
        "underline": False,
        "strikethrough": False,
        "superscript": False,
        "subscript": False,
        "hyperlink": None,
    }
    assert image == {
        "type": "image",
        "path": "assets/001 頁/a.png",
        "extension": ".png",
        "filename": "a.png",
        "alt_text": None,
    }
    assert missing["path"] is None
    assert [asset.relative_path for asset in allocator.assets] == ["assets/001 頁/a.png"]


def test_default_backup_dir_detects_localized_folder_names(tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "x.one").write_bytes(b"")
    assert default_backup_dir(tmp_path) == tmp_path / "備份"  # 都不存在時回傳預設名稱

    (tmp_path / "Sauvegarde" / "Notes").mkdir(parents=True)
    (tmp_path / "Sauvegarde" / "Notes" / "a.one").write_bytes(b"")
    assert default_backup_dir(tmp_path) == tmp_path / "Sauvegarde"

    (tmp_path / "Backup").mkdir()
    assert default_backup_dir(tmp_path) == tmp_path / "Backup"
