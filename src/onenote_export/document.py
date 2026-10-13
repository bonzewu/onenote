"""MS-ONE 語意層：把 revision store 的物件圖還原成 `model.Section`。

物件之間的主要關係（[MS-ONE] 2.2）：

    節物件空間
      jcidSectionNode ── ElementChildNodes ──▶ jcidPageSeriesNode
      jcidPageSeriesNode ── ChildGraphSpaceElementNodes ──▶ 每一頁的物件空間 ID
                         ── MetaDataObjectsAboveGraphSpace ──▶ jcidPageMetaData（標題、層級）
    頁面物件空間
      jcidPageManifestNode ── ContentChildNodes ──▶ jcidPageNode
      jcidPageNode ── StructureElementChildNodes／ElementChildNodes ──▶ 標題、大綱、圖片、附件
      jcidOutlineNode ── ElementChildNodes ──▶ jcidOutlineElementNode
      jcidOutlineElementNode ── ContentChildNodes ──▶ 文字／圖片／表格／附件
                             ── ElementChildNodes ──▶ 子段落（縮排）
                             ── ListNodes ──▶ jcidNumberListNode（清單符號）
"""

from __future__ import annotations

import bisect
import itertools
import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from .binary import ExtendedGUID, FormatError
from .model import (
    Attachment,
    Block,
    Image,
    ListMarker,
    NoteTag,
    Outline,
    OutlineItem,
    Page,
    PageElement,
    Paragraph,
    Section,
    Table,
    TableCell,
    TextRun,
    Unsupported,
)
from .onestore import ROLE_DEFAULT_CONTENT, ROLE_METADATA, ObjectEntry, ObjectSpace, OneStoreFile
from .properties import EMPTY_PROPERTY_SET, PropertyID, PropertySet, decode_property_set

log = logging.getLogger(__name__)


class JCID:
    """物件類型（[MS-ONE] 2.2）。"""

    SECTION = 0x00060007
    PAGE_SERIES = 0x00060008
    PAGE = 0x0006000B
    OUTLINE = 0x0006000C
    OUTLINE_ELEMENT = 0x0006000D
    RICH_TEXT = 0x0006000E
    IMAGE = 0x00060011
    NUMBER_LIST = 0x00060012
    OUTLINE_GROUP = 0x00060019
    TABLE = 0x00060022
    TABLE_ROW = 0x00060023
    TABLE_CELL = 0x00060024
    TITLE = 0x0006002C
    PAGE_METADATA = 0x00020030
    EMBEDDED_FILE = 0x00060035
    PAGE_MANIFEST = 0x00060037


# 已知但不轉換的物件類型 → 輸出中的提示名稱
UNSUPPORTED_KINDS = {
    0x00060014: "手寫筆跡",
    0x00060047: "手寫筆跡",
    0x0006003C: None,  # 版本歷程內容，不屬於頁面本體
    0x0006003D: None,
}

# OneNote 內建的「待辦事項」類核取方塊圖示代碼（[MS-ONE] 2.3.38 NoteTagShape）
CHECKBOX_SHAPES = frozenset({3, 8, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25})

# 零寬字元與私人使用區字元（網頁剪輯中圖示字型的符號）：離開原字型就無法顯示，
# 留在 Markdown 只會造成空連結或多餘空行
_INVISIBLE_CHARS = dict.fromkeys([*map(ord, "\u200b\u200c\u200d\u2060\ufeff"), *range(0xE000, 0xF900)])
HYPERLINK_FIELD = re.compile(r'\ufddfHYPERLINK\s+"([^"]*)"')
MAX_DEPTH = 64


# ---------------------------------------------------------------------------
# 時間轉換
# ---------------------------------------------------------------------------

_EPOCH_1601 = datetime(1601, 1, 1, tzinfo=UTC)
_EPOCH_1980 = datetime(1980, 1, 1, tzinfo=UTC)


def filetime_to_datetime(value: int | None) -> datetime | None:
    """FILETIME（自 1601-01-01 起的 100 奈秒數）轉為 UTC 時間。"""
    if not value:
        return None
    try:
        return _EPOCH_1601 + timedelta(microseconds=value // 10)
    except OverflowError:
        return None


def time32_to_datetime(value: int | None) -> datetime | None:
    """Time32（自 1980-01-01 起的秒數）轉為 UTC 時間。"""
    if not value:
        return None
    return _EPOCH_1980 + timedelta(seconds=value)


# ---------------------------------------------------------------------------
# 文字
# ---------------------------------------------------------------------------


def _decode_run_boundaries(raw: bytes | None) -> list[int]:
    if not raw:
        return []
    return [int.from_bytes(raw[i : i + 4], "little") for i in range(0, len(raw) - 3, 4)]


def split_runs(text: str, boundaries: list[int]) -> list[tuple[int, int]]:
    """依 TextRunIndex（每段的結束位置）切出各段文字的 (起點, 終點)；最後一段延伸到結尾。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for end in boundaries:
        end = max(start, min(end, len(text)))
        spans.append((start, end))
        start = end
    spans.append((start, len(text)))
    return spans


def clean_text(text: str) -> str:
    """去除 NUL、物件取代字元、欄位碼標記等不可見字元，並把 OneNote 的軟換行轉成 \\n。"""
    return (
        text.replace("\x00", "")
        .replace("\ufffc", "")
        .replace("\ufddf", "")
        .translate(_INVISIBLE_CHARS)
        .replace("\x0b", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def _style_flags(style: PropertySet) -> dict[str, bool]:
    return {
        "bold": style.flag(PropertyID.BOLD),
        "italic": style.flag(PropertyID.ITALIC),
        "underline": style.flag(PropertyID.UNDERLINE),
        "strikethrough": style.flag(PropertyID.STRIKETHROUGH),
        "superscript": style.flag(PropertyID.SUPERSCRIPT),
        "subscript": style.flag(PropertyID.SUBSCRIPT),
    }


def _link_ranges(
    text: str, spans: list[tuple[int, int]], styles: list[PropertySet]
) -> tuple[list[tuple[int, int]], list[tuple[int, int, str]]]:
    """找出 HYPERLINK 欄位碼的位置，以及每個超連結套用的顯示文字範圍。

    顯示文字從欄位碼結束處開始：若同一段文字還有剩餘就延伸到該段結尾，
    之後再延伸過連續標記為 Hyperlink 的文字段，直到下一個欄位碼為止。
    """
    fields = [(m.start(), m.end(), m.group(1)) for m in HYPERLINK_FIELD.finditer(text)]
    field_spans = [(start, end) for start, end, _ in fields]
    links: list[tuple[int, int, str]] = []

    def is_hyperlink(index: int) -> bool:
        return index < len(styles) and styles[index].flag(PropertyID.HYPERLINK)

    for position, (_, field_end, url) in enumerate(fields):
        limit = fields[position + 1][0] if position + 1 < len(fields) else len(text)
        index = bisect.bisect_right([start for start, _ in spans], field_end) - 1
        if index < 0 or field_end >= spans[index][1]:
            continue
        link_end = spans[index][1]
        index += 1
        while index < len(spans) and is_hyperlink(index) and spans[index][0] < limit:
            link_end = spans[index][1]
            index += 1
        links.append((field_end, min(link_end, limit), url))
    return field_spans, links


def build_runs(text: str, spans: list[tuple[int, int]], styles: list[PropertySet]) -> tuple[TextRun, ...]:
    """組合文字段與格式，並把 HYPERLINK 欄位碼轉成超連結。

    OneNote 的超連結存成兩部分：一段 `\\uFDDFHYPERLINK "網址"` 欄位碼（不顯示），
    後面跟著顯示文字；欄位碼可能被切在不同文字段中，所以以整段文字為單位處理。
    較新版本也可能直接在樣式上存 WzHyperlinkUrl。
    """
    field_spans, links = _link_ranges(text, spans, styles)
    cuts = sorted(
        {0, len(text)}
        | {point for span in spans for point in span}
        | {point for span in field_spans for point in span}
        | {point for start, end, _ in links for point in (start, end)}
    )

    span_starts = [start for start, _ in spans]
    field_starts = [start for start, _ in field_spans]
    link_starts = [start for start, _, _ in links]

    def containing(starts: list[int], position: int) -> int:
        """回傳起點 <= position 的最後一個區間索引（區間依起點排序且互不重疊）。"""
        return bisect.bisect_right(starts, position) - 1

    runs: list[TextRun] = []
    for start, end in itertools.pairwise(cuts):
        field = containing(field_starts, start)
        if field >= 0 and end <= field_spans[field][1]:
            continue
        index = max(containing(span_starts, start), 0)
        style = styles[index] if index < len(styles) else EMPTY_PROPERTY_SET
        piece = clean_text(text[start:end])
        if not piece:
            continue
        url = style.text(PropertyID.WZ_HYPERLINK_URL)
        link = containing(link_starts, start)
        if url is None and link >= 0 and end <= links[link][1]:
            url = links[link][2]
        runs.append(TextRun(text=piece, hyperlink=url or None, **_style_flags(style)))
    return tuple(runs)


def merge_runs(runs: Iterable[TextRun]) -> tuple[TextRun, ...]:
    """合併相鄰且格式相同的文字段。"""
    merged: list[TextRun] = []
    for run in runs:
        if merged and merged[-1].same_format(run):
            previous = merged[-1]
            merged[-1] = TextRun(
                previous.text + run.text,
                previous.bold,
                previous.italic,
                previous.underline,
                previous.strikethrough,
                previous.superscript,
                previous.subscript,
                previous.hyperlink,
            )
        else:
            merged.append(run)
    return tuple(merged)


def list_marker_from_format(fmt: str) -> ListMarker:
    """判斷 NumberListFormat 是編號清單還是項目符號。

    編號清單的格式字串含有 U+FFFD 之類的「數字佔位符」，項目符號則是單一符號字元。
    """
    cleaned = fmt.replace("\x00", "")
    ordered = "\ufffd" in cleaned or any(ch.isdigit() for ch in cleaned)
    return ListMarker(ordered=ordered, format=cleaned)


# ---------------------------------------------------------------------------
# 物件空間存取
# ---------------------------------------------------------------------------


class _SpaceView:
    """包裝一個物件空間，提供帶快取的屬性集讀取。"""

    def __init__(self, store: OneStoreFile, space: ObjectSpace) -> None:
        self.store = store
        self.space = space
        self._cache: dict[ExtendedGUID, PropertySet] = {}

    def entry(self, oid: ExtendedGUID | None) -> ObjectEntry | None:
        return self.space.objects.get(oid) if oid is not None else None

    def props(self, oid: ExtendedGUID | None) -> PropertySet:
        entry = self.entry(oid)
        if entry is None:
            return EMPTY_PROPERTY_SET
        cached = self._cache.get(entry.oid)
        if cached is None:
            try:
                cached = decode_property_set(self.store.data, entry)
            except FormatError as error:
                log.warning("物件 %s（jcid 0x%X）屬性解析失敗：%s", entry.oid, entry.jcid, error)
                cached = EMPTY_PROPERTY_SET
            self._cache[entry.oid] = cached
        return cached

    def jcid(self, oid: ExtendedGUID | None) -> int | None:
        entry = self.entry(oid)
        return entry.jcid if entry is not None else None

    def file_data(self, oid: ExtendedGUID | None) -> tuple[bytes | None, str | None]:
        entry = self.entry(oid)
        if entry is None:
            return None, None
        return self.store.read_file_data(entry.file_data_reference), entry.extension

    def children(self, oid: ExtendedGUID, prop: int) -> tuple[ExtendedGUID, ...]:
        return tuple(child for child in self.props(oid).ids(prop) if not child.is_nil)


# ---------------------------------------------------------------------------
# 頁面內容
# ---------------------------------------------------------------------------


class _PageBuilder:
    def __init__(self, view: _SpaceView) -> None:
        self.view = view
        self.warnings: list[str] = []
        self._visiting: set[ExtendedGUID] = set()

    def _guarded(self, oid: ExtendedGUID, depth: int, build: Callable[[], object]) -> object | None:
        """防止循環參照與過深的遞迴。"""
        if oid in self._visiting or depth > MAX_DEPTH:
            self.warnings.append(f"略過循環或過深的物件參照 {oid}")
            return None
        self._visiting.add(oid)
        try:
            return build()
        finally:
            self._visiting.discard(oid)

    # ---- 頁面層級元素 -----------------------------------------------------

    def page_elements(self, page_oid: ExtendedGUID) -> tuple[str | None, tuple[PageElement, ...]]:
        """回傳 (標題節點中的文字, 頁面元素)；元素依版面位置由上而下排序。"""
        view = self.view
        candidates: list[ExtendedGUID] = []
        for prop in (PropertyID.STRUCTURE_ELEMENT_CHILD_NODES, PropertyID.ELEMENT_CHILD_NODES):
            for child in view.children(page_oid, prop):
                if child not in candidates:
                    candidates.append(child)

        title_text: str | None = None
        positioned: list[tuple[float, float, int, PageElement]] = []
        for order, child in enumerate(candidates):
            jcid = view.jcid(child)
            if jcid == JCID.TITLE:
                title_text = title_text or self._title_text(child)
                continue
            element = self._page_element(child, jcid)
            if element is None:
                continue
            props = view.props(child)
            vertical = props.float32(PropertyID.OFFSET_FROM_PARENT_VERT) or 0.0
            horizontal = props.float32(PropertyID.OFFSET_FROM_PARENT_HORIZ) or 0.0
            positioned.append((vertical, horizontal, order, element))

        positioned.sort(key=lambda item: (item[0], item[1], item[2]))
        return title_text, tuple(element for *_, element in positioned)

    def _page_element(self, oid: ExtendedGUID, jcid: int | None) -> PageElement | None:
        match jcid:
            case JCID.OUTLINE:
                return Outline(self.outline_items(oid, 0))
            case JCID.IMAGE:
                return self.image(oid)
            case JCID.EMBEDDED_FILE:
                return self.attachment(oid)
            case None:
                return None
            case _:
                return self._unsupported(jcid)

    def _unsupported(self, jcid: int) -> Unsupported | None:
        if jcid in UNSUPPORTED_KINDS:
            kind = UNSUPPORTED_KINDS[jcid]
            return Unsupported(kind) if kind else None
        return Unsupported(f"jcid 0x{jcid:08X}")

    def _title_text(self, title_oid: ExtendedGUID) -> str | None:
        texts: list[str] = []
        for outline in self.view.children(title_oid, PropertyID.ELEMENT_CHILD_NODES):
            for item in self.outline_items(outline, 0):
                texts.extend(block.plain_text.strip() for block in item.content if isinstance(block, Paragraph))
        joined = " ".join(text for text in texts if text)
        return joined or None

    # ---- 大綱 -------------------------------------------------------------

    def outline_items(self, container: ExtendedGUID, depth: int) -> tuple[OutlineItem, ...]:
        items: list[OutlineItem] = []
        for child in self.view.children(container, PropertyID.ELEMENT_CHILD_NODES):
            match self.view.jcid(child):
                case JCID.OUTLINE_ELEMENT:
                    item = self._guarded(child, depth, lambda c=child: self.outline_item(c, depth))
                    if isinstance(item, OutlineItem):
                        items.append(item)
                case JCID.OUTLINE_GROUP:
                    group = self._guarded(child, depth, lambda c=child: self.outline_items(c, depth + 1))
                    if isinstance(group, tuple):
                        items.extend(group)
                case _:
                    pass
        return tuple(items)

    def outline_item(self, oid: ExtendedGUID, depth: int) -> OutlineItem:
        view = self.view
        props = view.props(oid)
        content = tuple(
            block
            for child in view.children(oid, PropertyID.CONTENT_CHILD_NODES)
            if (block := self.block(child, depth)) is not None
        )
        children = self.outline_items(oid, depth + 1)

        marker = None
        list_oid = props.first_id(PropertyID.LIST_NODES)
        if list_oid is not None:
            fmt = view.props(list_oid).text(PropertyID.NUMBER_LIST_FORMAT) or ""
            marker = list_marker_from_format(fmt)

        tags = tuple(self._note_tag(state) for state in props.property_sets(PropertyID.NOTE_TAG_STATES))
        return OutlineItem(content=content, children=children, list_marker=marker, tags=tags)

    def _note_tag(self, state: PropertySet) -> NoteTag:
        definition = self.view.props(state.first_id(PropertyID.NOTE_TAG_DEFINITION_OID))
        shape = definition.uint(PropertyID.NOTE_TAG_SHAPE)
        return NoteTag(
            label=definition.text(PropertyID.NOTE_TAG_LABEL),
            is_checkbox=shape in CHECKBOX_SHAPES,
            completed=state.has(PropertyID.NOTE_TAG_COMPLETED),
        )

    # ---- 內容區塊 ---------------------------------------------------------

    def block(self, oid: ExtendedGUID, depth: int) -> Block | None:
        match self.view.jcid(oid):
            case JCID.RICH_TEXT:
                return self.paragraph(oid)
            case JCID.IMAGE:
                return self.image(oid)
            case JCID.EMBEDDED_FILE:
                return self.attachment(oid)
            case JCID.TABLE:
                table = self._guarded(oid, depth, lambda: self.table(oid, depth))
                return table if isinstance(table, Table) else None
            case None:
                return None
            case jcid:
                return self._unsupported(jcid)

    def paragraph(self, oid: ExtendedGUID) -> Paragraph:
        view = self.view
        props = view.props(oid)
        unicode_raw = props.raw(PropertyID.RICH_EDIT_TEXT_UNICODE)
        if unicode_raw is not None:
            text = unicode_raw.decode("utf-16-le", errors="replace")
        else:
            ascii_raw = props.raw(PropertyID.TEXT_EXTENDED_ASCII) or b""
            text = ascii_raw.decode("cp1252", errors="replace")

        boundaries = _decode_run_boundaries(props.raw(PropertyID.TEXT_RUN_INDEX))
        styles = [view.props(style) for style in props.ids(PropertyID.TEXT_RUN_FORMATTING)]
        runs = merge_runs(build_runs(text, split_runs(text, boundaries), styles))

        paragraph_style = view.props(props.first_id(PropertyID.PARAGRAPH_STYLE))
        style_id = paragraph_style.text(PropertyID.PARAGRAPH_STYLE_ID)
        return Paragraph(runs=runs, style=style_id or None)

    def image(self, oid: ExtendedGUID) -> Image:
        props = self.view.props(oid)
        data, extension = self.view.file_data(props.first_id(PropertyID.PICTURE_CONTAINER))
        if data is None:
            self.warnings.append("圖片資料不在備份檔中，已略過內容")
        return Image(
            data=data,
            extension=(extension or ".png").strip("\x00") or ".png",
            filename=props.text(PropertyID.IMAGE_FILENAME) or None,
            alt_text=props.text(PropertyID.IMAGE_ALT_TEXT) or None,
        )

    def attachment(self, oid: ExtendedGUID) -> Attachment:
        props = self.view.props(oid)
        data, _ = self.view.file_data(props.first_id(PropertyID.EMBEDDED_FILE_CONTAINER))
        if data is None:
            self.warnings.append("附件資料不在備份檔中，已略過內容")
        return Attachment(data=data, filename=props.text(PropertyID.EMBEDDED_FILE_NAME) or "attachment")

    def table(self, oid: ExtendedGUID, depth: int) -> Table:
        view = self.view
        rows: list[tuple[TableCell, ...]] = []
        for row in view.children(oid, PropertyID.ELEMENT_CHILD_NODES):
            if view.jcid(row) != JCID.TABLE_ROW:
                continue
            cells = tuple(
                TableCell(self.outline_items(cell, depth + 1))
                for cell in view.children(row, PropertyID.ELEMENT_CHILD_NODES)
                if view.jcid(cell) == JCID.TABLE_CELL
            )
            rows.append(cells)
        return Table(tuple(rows))


# ---------------------------------------------------------------------------
# 節
# ---------------------------------------------------------------------------


def _page_from_space(store: OneStoreFile, space: ObjectSpace, section_metadata: PropertySet) -> Page:
    view = _SpaceView(store, space)
    builder = _PageBuilder(view)

    page_oid: ExtendedGUID | None = None
    manifest = space.root(ROLE_DEFAULT_CONTENT)
    if manifest is not None and manifest.jcid == JCID.PAGE_MANIFEST:
        page_oid = next(
            (
                child
                for child in view.children(manifest.oid, PropertyID.CONTENT_CHILD_NODES)
                if view.jcid(child) == JCID.PAGE
            ),
            None,
        )
    elif manifest is not None and manifest.jcid == JCID.PAGE:
        page_oid = manifest.oid

    metadata_entry = space.root(ROLE_METADATA)
    metadata = view.props(metadata_entry.oid) if metadata_entry is not None else EMPTY_PROPERTY_SET

    title_from_node: str | None = None
    elements: tuple[PageElement, ...] = ()
    page_props = EMPTY_PROPERTY_SET
    if page_oid is not None:
        page_props = view.props(page_oid)
        title_from_node, elements = builder.page_elements(page_oid)
    else:
        builder.warnings.append("找不到頁面內容節點")

    title = (
        metadata.text(PropertyID.CACHED_TITLE_STRING)
        or section_metadata.text(PropertyID.CACHED_TITLE_STRING)
        or title_from_node
        or "未命名頁面"
    )
    level = metadata.uint(PropertyID.PAGE_LEVEL) or section_metadata.uint(PropertyID.PAGE_LEVEL) or 1
    created = filetime_to_datetime(
        metadata.uint(PropertyID.TOPOLOGY_CREATION_TIMESTAMP)
        or section_metadata.uint(PropertyID.TOPOLOGY_CREATION_TIMESTAMP)
    )
    modified = time32_to_datetime(page_props.uint(PropertyID.LAST_MODIFIED_TIME))

    return Page(
        title=clean_text(title).strip() or "未命名頁面",
        level=level,
        created=created,
        modified=modified,
        author=page_props.text(PropertyID.AUTHOR) or None,
        elements=elements,
        warnings=tuple(builder.warnings),
    )


def build_section(store: OneStoreFile, name: str) -> Section:
    """把整個 `.one` 檔還原成一個節。"""
    if not store.header.is_section:
        raise FormatError("這是筆記本目錄檔（.onetoc2），不含頁面內容")

    root_view = _SpaceView(store, store.root_space)
    section = store.root_space.root(ROLE_DEFAULT_CONTENT)
    if section is None:
        return Section(name=name)

    pages: list[Page] = []
    for series in root_view.children(section.oid, PropertyID.ELEMENT_CHILD_NODES):
        if root_view.jcid(series) != JCID.PAGE_SERIES:
            continue
        page_spaces = root_view.props(series).ids(PropertyID.CHILD_GRAPH_SPACE_ELEMENT_NODES)
        metadata_ids = root_view.props(series).ids(PropertyID.META_DATA_OBJECTS_ABOVE_GRAPH_SPACE)
        for index, gosid in enumerate(page_spaces):
            space = store.object_spaces.get(gosid)
            section_metadata = root_view.props(metadata_ids[index]) if index < len(metadata_ids) else EMPTY_PROPERTY_SET
            if space is None:
                # 節的目錄有記錄這一頁，但內容沒有寫進這個檔案（常見於尚未同步完成的頁面）
                title = clean_text(section_metadata.text(PropertyID.CACHED_TITLE_STRING) or "未命名頁面")
                log.warning("節「%s」頁面「%s」的內容不在檔案中", name, title)
                pages.append(
                    Page(
                        title=title.strip() or "未命名頁面",
                        level=section_metadata.uint(PropertyID.PAGE_LEVEL) or 1,
                        created=filetime_to_datetime(section_metadata.uint(PropertyID.TOPOLOGY_CREATION_TIMESTAMP)),
                        warnings=("頁面內容不在備份檔中，只保留標題",),
                    )
                )
                continue
            try:
                pages.append(_page_from_space(store, space, section_metadata))
            except FormatError as error:
                title = section_metadata.text(PropertyID.CACHED_TITLE_STRING) or "未命名頁面"
                log.warning("節「%s」頁面「%s」解析失敗：%s", name, title, error)
                pages.append(Page(title=clean_text(title), warnings=(f"頁面解析失敗：{error}",)))

    return Section(name=name, pages=tuple(pages))
