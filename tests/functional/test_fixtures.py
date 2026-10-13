"""功能測試：解析 Apache Tika 的真實 OneNote 檔案。

預期文字取自 Tika 的 OneNoteParserTest，確保與另一個獨立實作的結果一致。
"""

import contextlib
from collections.abc import Iterator

import pytest

from onenote_export.binary import FormatError
from onenote_export.document import build_section
from onenote_export.model import (
    Attachment,
    Image,
    Outline,
    OutlineItem,
    Paragraph,
    Section,
    Table,
)
from onenote_export.onestore import UnsupportedFileError, parse_onestore


def walk_items(items: tuple[OutlineItem, ...]) -> Iterator[OutlineItem]:
    for item in items:
        yield item
        for block in item.content:
            if isinstance(block, Table):
                for row in block.rows:
                    for cell in row:
                        yield from walk_items(cell.items)
        yield from walk_items(item.children)


def all_blocks(section: Section):
    for page in section.pages:
        for element in page.elements:
            if isinstance(element, Outline):
                for item in walk_items(element.items):
                    yield from item.content
            else:
                yield element


def section_text(section: Section) -> str:
    return "\n".join(block.plain_text for block in all_blocks(section) if isinstance(block, Paragraph))


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("testOneNote3.one", ["Section2TextArea1", "Section2HeaderTitle", "Section2TextArea2"]),
        ("testOneNote2016.one", ["So good", "This is one note 2016"]),
        ("test-tika-4303-Chinese-notes.one", ["中文标题", "OneNote 是一款数字笔记本"]),
    ],
)
def test_expected_text_is_extracted(section_loader, fixture, expected):
    section = section_loader(fixture)
    text = section_text(section) + "\n" + "\n".join(page.title for page in section.pages)
    for snippet in expected:
        assert snippet in text


def test_testonenote1_structure(section_loader):
    section = section_loader("testOneNote1.one")
    assert [page.title for page in section.pages] == ["OneNote: one place for all of your notes", "OneNote Basics"]
    first = section.pages[0]
    assert first.author == "Microsoft"
    assert first.created is not None and first.created.year == 2012
    assert first.modified is not None and first.modified.year == 2015

    blocks = list(all_blocks(section))
    images = [block for block in blocks if isinstance(block, Image)]
    tables = [block for block in blocks if isinstance(block, Table)]
    assert len(images) == 36
    assert all(image.data and image.data.startswith(b"\x89PNG") for image in images)
    assert len(tables) >= 3

    ordered = [
        item
        for element in first.elements
        if isinstance(element, Outline)
        for item in walk_items(element.items)
        if item.list_marker and item.list_marker.ordered
    ]
    assert [item.content[0].plain_text for item in ordered] == [
        "For more tips, check out 30 second videos",
        "Create your first page",
    ]


def test_hyperlinks_are_extracted(section_loader):
    section = section_loader("testOneNote1.one")
    links = {
        run.hyperlink
        for block in all_blocks(section)
        if isinstance(block, Paragraph)
        for run in block.runs
        if run.hyperlink
    }
    assert "http://o15.officeredir.microsoft.com/r/rlidOneNoteGuideVideo15?clid=1033" in links
    assert not any("HYPERLINK" in block.plain_text for block in all_blocks(section) if isinstance(block, Paragraph))


def test_bold_text_is_preserved(section_loader):
    section = section_loader("testOneNote3.one")
    bold = [run.text for block in all_blocks(section) if isinstance(block, Paragraph) for run in block.runs if run.bold]
    assert "totally killin it bro" in "".join(bold)


def test_embedded_word_document_is_extracted(section_loader):
    section = section_loader("testOneNoteEmbeddedWordDoc.one")
    (attachment,) = [block for block in all_blocks(section) if isinstance(block, Attachment)]
    assert attachment.filename == "Dude this is a super cool embedded doc.docx"
    assert attachment.data is not None and attachment.data.startswith(b"PK\x03\x04")  # docx 是 zip


def test_chinese_bullet_list(section_loader):
    section = section_loader("test-tika-4303-Chinese-notes.one")
    bullets = [
        item.content[0].plain_text
        for element in section.pages[0].elements
        if isinstance(element, Outline)
        for item in walk_items(element.items)
        if item.list_marker and not item.list_marker.ordered
    ]
    assert bullets[0] == "向笔记本中键入信息或从其他应用和网页插入信息。"
    assert len(bullets) == 5


def test_fsshttpb_packaging_is_reported_as_unsupported(fixtures_dir):
    with pytest.raises(UnsupportedFileError, match="FSSHTTPB"):
        parse_onestore((fixtures_dir / "testOneNoteFromOffice365.one").read_bytes())


def test_corrupted_toc_file_fails_safely(fixtures_dir):
    store = parse_onestore((fixtures_dir / "testOneNote-fuzz1.one").read_bytes())
    with pytest.raises(FormatError, match="onetoc2"):
        build_section(store, "fuzz")


@pytest.mark.parametrize("cut", [1024, 4096, 20000])
def test_truncated_files_never_crash_with_unexpected_errors(fixtures_dir, cut):
    data = (fixtures_dir / "testOneNote1.one").read_bytes()[:cut]
    # 損毀檔案只能以 FormatError 失敗，不可出現其他例外
    with contextlib.suppress(FormatError):
        build_section(parse_onestore(data), "truncated")
