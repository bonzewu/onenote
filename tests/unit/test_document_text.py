"""document.py：文字段切分、超連結欄位碼、清單符號、時間轉換。"""

from datetime import UTC, datetime, timedelta

from onenote_export.document import (
    build_runs,
    clean_text,
    filetime_to_datetime,
    list_marker_from_format,
    merge_runs,
    split_runs,
    time32_to_datetime,
)
from onenote_export.model import TextRun
from onenote_export.properties import EMPTY_PROPERTY_SET, PropertyID, PropertySet

HYPERLINK = PropertySet({PropertyID.HYPERLINK: True})
BOLD = PropertySet({PropertyID.BOLD: True})
PLAIN = EMPTY_PROPERTY_SET


def test_split_runs_uses_end_positions_and_extends_last_run():
    assert split_runs("abcdef", [2, 4]) == [(0, 2), (2, 4), (4, 6)]


def test_split_runs_clamps_out_of_range_boundaries():
    assert split_runs("abc", [5, 1]) == [(0, 3), (3, 3), (3, 3)]


def test_clean_text_removes_invisible_characters_and_normalizes_breaks():
    text = "a\x00b\ufffc\u200bc\x0bd\r\ne\ue620"
    assert clean_text(text) == "abc\nd\ne"


def test_build_runs_applies_formatting_per_run():
    runs = build_runs("粗體一般", split_runs("粗體一般", [2]), [BOLD, PLAIN])
    assert runs == (TextRun("粗體", bold=True), TextRun("一般"))


def test_hyperlink_field_code_in_separate_run():
    text = '\ufddfHYPERLINK "https://example.com"範例網站'
    field_end = text.index("範")
    runs = build_runs(text, split_runs(text, [field_end]), [PLAIN, HYPERLINK])
    assert runs == (TextRun("範例網站", hyperlink="https://example.com"),)


def test_hyperlink_field_code_split_across_runs():
    """回歸測試：欄位碼被切在兩個文字段中，舊版會殘留「HYPERLINK "...」文字。"""
    text = '前文\ufddfHYPERLINK "https://example.com/a_b"連結後文'
    start = text.index("\ufddf")
    middle = text.index("example")
    display = text.index("連結")
    after = text.index("後文")
    spans = split_runs(text, [start, middle, display, after])
    runs = build_runs(text, spans, [PLAIN, PLAIN, PLAIN, HYPERLINK, PLAIN])
    assert [(run.text, run.hyperlink) for run in runs] == [
        ("前文", None),
        ("連結", "https://example.com/a_b"),
        ("後文", None),
    ]


def test_hyperlink_display_text_in_same_run_as_field_code():
    text = '\ufddfHYPERLINK "https://example.com"https://example.com'
    runs = build_runs(text, split_runs(text, []), [PLAIN])
    assert runs == (TextRun("https://example.com", hyperlink="https://example.com"),)


def test_style_url_takes_precedence():
    style = PropertySet({PropertyID.WZ_HYPERLINK_URL: "https://direct.example/\x00".encode("utf-16-le")})
    runs = build_runs("文字", split_runs("文字", []), [style])
    assert runs[0].hyperlink == "https://direct.example/"


def test_merge_runs_joins_adjacent_runs_with_same_format():
    runs = merge_runs([TextRun("a"), TextRun("b"), TextRun("c", bold=True), TextRun("d", bold=True), TextRun("e")])
    assert runs == (TextRun("ab"), TextRun("cd", bold=True), TextRun("e"))


def test_list_marker_detection():
    assert list_marker_from_format("\x03\ufffd.").ordered is True
    assert list_marker_from_format("\x01•").ordered is False
    assert list_marker_from_format("\x01▪").ordered is False


def test_time_conversions():
    expected = datetime(2020, 1, 24, 14, 29, 53, 868999, tzinfo=UTC)
    # FILETIME：自 1601-01-01 起的 100 奈秒數
    filetime = ((expected - datetime(1601, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)) * 10
    assert filetime_to_datetime(filetime) == expected
    assert time32_to_datetime(60) == datetime(1980, 1, 1, 0, 1, tzinfo=UTC)
    assert filetime_to_datetime(0) is None
    assert time32_to_datetime(None) is None
    assert filetime_to_datetime(2**64 - 1) is None
