"""markdown.py：行內格式、跳脫、清單、表格、圖片與附件。"""

import json
from datetime import UTC, datetime

import pytest

from onenote_export.markdown import (
    escape_line_start,
    escape_text,
    front_matter,
    preserve_indentation,
    render_page,
    render_run,
)
from onenote_export.model import (
    Attachment,
    Image,
    ListMarker,
    NoteTag,
    Outline,
    OutlineItem,
    Page,
    Paragraph,
    Table,
    TableCell,
    TextRun,
    Unsupported,
)

BULLET = ListMarker(ordered=False)
NUMBER = ListMarker(ordered=True)


def para(text: str, style: str | None = None, **formats) -> Paragraph:
    return Paragraph((TextRun(text, **formats),), style=style)


def item(text: str, marker: ListMarker | None = None, children=(), tags=()) -> OutlineItem:
    return OutlineItem(content=(para(text),), children=tuple(children), list_marker=marker, tags=tuple(tags))


def body(*elements, title: str = "標題") -> str:
    """只取頁面標題之後的內容，方便比對。"""
    markdown = render_page(Page(title=title, elements=tuple(elements)), "assets/page").markdown
    return markdown.split(f"# {title}\n\n", 1)[1] if f"# {title}\n\n" in markdown else ""


# ---------------------------------------------------------------------------
# 行內
# ---------------------------------------------------------------------------


def test_escape_text_escapes_markdown_syntax_but_keeps_urls():
    assert escape_text("a*b_c [x] `y` <div> 3 > 2") == "a\\*b\\_c \\[x\\] \\`y\\` \\<div> 3 > 2"
    assert escape_text("見 https://example.com/a_b*c 說明") == "見 https://example.com/a_b*c 說明"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("# 不是標題", "\\# 不是標題"),
        ("- 不是清單", "\\- 不是清單"),
        ("> 不是引言", "\\> 不是引言"),
        ("1. 不是編號", "1\\. 不是編號"),
        ("12) 也不是", "12\\) 也不是"),
        ("#hashtag", "#hashtag"),
        ("一般文字 - 中間", "一般文字 - 中間"),
    ],
)
def test_escape_line_start(line, expected):
    assert escape_line_start(line) == expected


def test_preserve_indentation_avoids_code_blocks():
    assert preserve_indentation("    print(1)") == "&nbsp;&nbsp;&nbsp;&nbsp;print(1)"
    assert preserve_indentation("\tx") == "&nbsp;" * 4 + "x"
    assert preserve_indentation("x  y") == "x  y"


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (TextRun(" 粗體 ", bold=True), " **粗體** "),
        (TextRun("斜", italic=True), "*斜*"),
        (TextRun("粗斜", bold=True, italic=True), "***粗斜***"),
        (TextRun("刪", strikethrough=True), "~~刪~~"),
        (TextRun("底線", underline=True), "<u>底線</u>"),
        (TextRun("2", superscript=True), "<sup>2</sup>"),
        (TextRun("i", subscript=True), "<sub>i</sub>"),
        (TextRun("網站", hyperlink="https://e.com/"), "[網站](https://e.com/)"),
        (TextRun("網站", underline=True, hyperlink="https://e.com/"), "[網站](https://e.com/)"),
        (TextRun("https://e.com/", hyperlink="https://e.com/"), "https://e.com/"),
        (TextRun("檔案", hyperlink="file:///a b/c(1).txt"), "[檔案](<file:///a b/c(1).txt>)"),
        (TextRun("錨點", hyperlink="#top"), "錨點"),
        (TextRun(" ", hyperlink="https://e.com/"), " <https://e.com/>"),
        (TextRun("   ", bold=True), "   "),
    ],
)
def test_render_run(run, expected):
    assert render_run(run) == expected


# ---------------------------------------------------------------------------
# 段落與標題
# ---------------------------------------------------------------------------


def test_paragraphs_are_separated_by_blank_lines():
    assert body(Outline((item("第一段"), item("第二段")))) == "第一段\n\n第二段\n"


def test_soft_line_breaks_become_hard_breaks():
    assert body(Outline((item("第一行\n第二行\n\n\n第三行\n"),))) == "第一行\\\n第二行\n\n第三行\n"


def test_onenote_headings_shift_down_one_level():
    outline = Outline((OutlineItem((para("大標", style="h1"),)), OutlineItem((para("小標", style="h6"),))))
    assert body(outline) == "## 大標\n\n###### 小標\n"


def test_heading_inside_list_becomes_bold():
    outline = Outline((OutlineItem((para("清單中的標題", style="h2"),), list_marker=BULLET),))
    assert body(outline) == "- **清單中的標題**\n"


def test_quote_code_and_cite_styles():
    outline = Outline(
        (
            OutlineItem((para("引言", style="quote"),)),
            OutlineItem((para("x = 1", style="code"),)),
            OutlineItem((para("出處", style="cite"),)),
        )
    )
    assert body(outline) == "> 引言\n\n`x = 1`\n\n*出處*\n"


def test_empty_paragraphs_are_skipped():
    assert body(Outline((item(""), item("   "), item("內容")))) == "內容\n"


# ---------------------------------------------------------------------------
# 清單
# ---------------------------------------------------------------------------


def test_bullet_and_numbered_lists_are_tight():
    outline = Outline((item("甲", NUMBER), item("乙", NUMBER), item("丙", BULLET)))
    assert body(outline) == "1. 甲\n2. 乙\n- 丙\n"


def test_nested_lists_use_continuation_indent():
    outline = Outline((item("父", BULLET, children=[item("子", NUMBER, children=[item("孫", BULLET)])]),))
    assert body(outline) == "- 父\n  1. 子\n     - 孫\n"


def test_indented_plain_paragraphs_become_nested_bullets():
    outline = Outline((item("一般段落", children=[item("縮排段落")]),))
    assert body(outline) == "一般段落\n\n- 縮排段落\n"


def test_checkbox_tags_render_as_task_list():
    checkbox = NoteTag(label="待辦事項", is_checkbox=True, completed=False)
    done = NoteTag(label="待辦事項", is_checkbox=True, completed=True)
    outline = Outline((item("買牛奶", tags=[checkbox]), item("繳電費", tags=[done])))
    assert body(outline) == "- [ ] 買牛奶\n- [x] 繳電費\n"


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------


def cell(*texts: str) -> TableCell:
    return TableCell(tuple(item(text) for text in texts))


def test_table_uses_first_row_as_header_and_pads_rows():
    table = Table(((cell("名稱"), cell("數量")), (cell("蘋果"),), (cell("a|b"), cell("第一行", "第二行"))))
    assert body(Outline((OutlineItem((table,)),))) == (
        "| 名稱 | 數量 |\n| --- | --- |\n| 蘋果 |  |\n| a\\|b | 第一行<br>第二行 |\n"
    )


def test_table_inside_list_item_starts_on_its_own_line():
    table = Table(((cell("a"), cell("b")),))
    outline = Outline((OutlineItem((table,), list_marker=BULLET),))
    assert body(outline) == "-\n  | a | b |\n  | --- | --- |\n"


def test_nested_table_is_flattened_into_cell_text():
    inner = Table(((cell("x"), cell("y")), (cell("z"),)))
    outer = Table(((TableCell((OutlineItem((inner,)),)),),))
    assert body(Outline((OutlineItem((outer,)),))) == "| x ／ y<br>z |\n| --- |\n"


def test_empty_table_is_omitted():
    assert body(Outline((OutlineItem((Table(()),)), item("後面")))) == "後面\n"


# ---------------------------------------------------------------------------
# 圖片、附件、不支援內容
# ---------------------------------------------------------------------------


def test_images_are_saved_as_assets_with_unique_names():
    first = Image(b"\x89PNG1", ".png", filename="圖.png", alt_text="說明")
    second = Image(b"\x89PNG2", ".png", filename="圖.png")
    rendered = render_page(Page(title="t", elements=(first, second)), "assets/001 t")
    assert "![說明](<assets/001 t/圖.png>)" in rendered.markdown
    assert "![圖.png](<assets/001 t/圖 (2).png>)" in rendered.markdown
    assert [(a.relative_path, a.data) for a in rendered.assets] == [
        ("assets/001 t/圖.png", b"\x89PNG1"),
        ("assets/001 t/圖 (2).png", b"\x89PNG2"),
    ]


def test_missing_image_and_attachment_render_placeholders():
    text = body(Image(None, ".jpeg", filename="照片.jpeg"), Attachment(None, "報告.pdf"))
    assert text == "*［圖片未包含在備份檔中：照片.jpeg］*\n\n*［附件未包含在備份檔中：報告.pdf］*\n"


def test_attachment_link_and_unsupported_content():
    rendered = render_page(
        Page(title="t", elements=(Attachment(b"PK", "a b.docx"), Unsupported("手寫筆跡"))), "assets/p"
    )
    assert "附件：[a b.docx](<assets/p/a b.docx>)" in rendered.markdown
    assert "*［未匯出的內容：手寫筆跡］*" in rendered.markdown


# ---------------------------------------------------------------------------
# 頁面
# ---------------------------------------------------------------------------


def test_front_matter_is_valid_yaml_with_escaped_strings():
    page = Page(
        title='含 "引號" 與: 冒號',
        level=2,
        created=datetime(2020, 1, 24, 14, 29, 53, tzinfo=UTC),
        author="作者",
    )
    lines = front_matter(page, section="節", notebook="筆記本").splitlines()
    assert lines[0] == lines[-1] == "---"
    fields = {line.split(": ", 1)[0]: json.loads(line.split(": ", 1)[1]) for line in lines[1:-1]}
    assert fields["title"] == '含 "引號" 與: 冒號'
    assert fields["level"] == 2
    assert fields["section"] == "節"
    assert fields["notebook"] == "筆記本"
    assert datetime.fromisoformat(fields["created"]) == page.created
    assert "modified" not in fields


def test_page_without_content_explains_why():
    page = Page(title="遺失的頁面", warnings=("頁面內容不在備份檔中，只保留標題",))
    assert "> 這一頁的內容不在備份檔中" in render_page(page, "assets/x").markdown


def test_title_is_escaped():
    assert "# \\*重要\\* 筆記\n" in render_page(Page(title="*重要* 筆記"), "a").markdown
