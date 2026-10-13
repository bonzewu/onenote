"""naming.py：檔名清理、備份檔名解析、唯一檔名、附件配置。"""

from datetime import date

import pytest

from onenote_export.naming import AssetAllocator, UniqueNamer, parse_backup_name, safe_filename, strip_invisible


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("一般 標題", "一般 標題"),
        ('a/b\\c:d*e?f"g<h>i|j', "a b c d e f g h i j"),
        ("  前後空白與句點. ", "前後空白與句點"),
        ("\ufffc\ufffc重要文件", "重要文件"),
        ("多個   空白\t\n換行", "多個 空白 換行"),
        ("CON", "_CON"),
        ("", "untitled"),
        ("///", "untitled"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_truncates_long_names():
    assert len(safe_filename("字" * 200, max_length=60)) == 60


def test_strip_invisible():
    assert strip_invisible("a\u200bb\ufeffc\ufffc") == "abc"


@pytest.mark.parametrize(
    ("filename", "section", "backup_date"),
    [
        ("備忘.one (2026-9-27).one", "備忘", date(2026, 9, 27)),
        ("新的節 1 2.one (2023-9-24).one", "新的節 1 2", date(2023, 9, 24)),
        ("\ufffc\ufffc重要文件.one (2026-3-21).one", "重要文件", date(2026, 3, 21)),
        ("一般節.one", "一般節", None),
        ("壞日期.one (2026-13-40).one", "壞日期", None),
    ],
)
def test_parse_backup_name(filename, section, backup_date):
    parsed = parse_backup_name(filename)
    assert (parsed.section, parsed.backup_date) == (section, backup_date)


def test_unique_namer_is_case_insensitive():
    namer = UniqueNamer()
    assert namer.claim("Page", ".md") == "Page.md"
    assert namer.claim("page", ".md") == "page (2).md"
    assert namer.claim("PAGE", ".md") == "PAGE (3).md"


def test_asset_allocator_uses_suggested_name_and_default_extension():
    allocator = AssetAllocator("assets/頁")
    assert allocator.add(b"1", "截圖.PNG", "image", ".png") == "assets/頁/截圖.png"
    assert allocator.add(b"2", None, "image", ".jpeg") == "assets/頁/image.jpeg"
    assert allocator.add(b"3", "無副檔名", "attachment", "") == "assets/頁/無副檔名"
    assert [asset.data for asset in allocator.assets] == [b"1", b"2", b"3"]
