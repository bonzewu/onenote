"""屬性集（property set）解碼，以及 MS-ONE 屬性 ID 常數。

每個物件的內容存放在 ObjectSpaceObjectPropSet（[MS-ONESTORE] 2.6.1）：

    OIDs 串流（物件參照用的 CompactID 陣列）
    OSIDs 串流（物件空間參照，可省略）
    ContextIDs 串流（可省略）
    PropertySet：屬性 ID 陣列 + 各屬性資料

屬性值中的物件參照不直接存 ID，而是依序從上述串流取出，
因此巢狀的屬性集也共用同一組串流游標。
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .binary import ExtendedGUID, FormatError, Reader
from .onestore import CompactID, GlobalIdTable, ObjectEntry, resolve_compact_id

# 屬性值的可能型別
type PropertyValue = bool | int | bytes | tuple[ExtendedGUID, ...] | PropertySet | tuple[PropertySet, ...] | None


class PropertyType:
    """PropertyID 中第 26–30 位元代表的資料型別（[MS-ONESTORE] 2.6.6）。"""

    NO_DATA = 0x1
    BOOL = 0x2
    ONE_BYTE = 0x3
    TWO_BYTES = 0x4
    FOUR_BYTES = 0x5
    EIGHT_BYTES = 0x6
    FOUR_BYTES_LENGTH_DATA = 0x7
    OBJECT_ID = 0x8
    ARRAY_OF_OBJECT_IDS = 0x9
    OBJECT_SPACE_ID = 0xA
    ARRAY_OF_OBJECT_SPACE_IDS = 0xB
    CONTEXT_ID = 0xC
    ARRAY_OF_CONTEXT_IDS = 0xD
    ARRAY_OF_PROPERTY_VALUES = 0x10
    PROPERTY_SET = 0x11


class PropertyID:
    """本工具使用到的 MS-ONE 屬性 ID（已含型別位元）。"""

    PAGE_WIDTH = 0x14001C01
    PAGE_HEIGHT = 0x14001C02
    OUTLINE_ELEMENT_CHILD_LEVEL = 0x0C001C03
    BOLD = 0x08001C04
    ITALIC = 0x08001C05
    UNDERLINE = 0x08001C06
    STRIKETHROUGH = 0x08001C07
    SUPERSCRIPT = 0x08001C08
    SUBSCRIPT = 0x08001C09
    FONT = 0x1C001C0A
    FONT_SIZE = 0x10001C0B
    OFFSET_FROM_PARENT_HORIZ = 0x14001C14
    OFFSET_FROM_PARENT_VERT = 0x14001C15
    NUMBER_LIST_FORMAT = 0x1C001C1A
    CONTENT_CHILD_NODES = 0x24001C1F
    ELEMENT_CHILD_NODES = 0x24001C20
    RICH_EDIT_TEXT_UNICODE = 0x1C001C22
    LIST_NODES = 0x24001C26
    PICTURE_CONTAINER = 0x20001C3F
    TOPOLOGY_CREATION_TIMESTAMP = 0x18001C65
    IS_TITLE_TIME = 0x08001C87
    IS_TITLE_TEXT = 0x08001CB4
    IS_TITLE_DATE = 0x08001CB5
    LIST_RESTART = 0x14001CB7
    CACHED_TITLE_STRING = 0x1C001CF3
    RICH_EDIT_TEXT_LANG_ID = 0x10001CFE
    CREATION_TIMESTAMP = 0x14001D09
    CACHED_TITLE_STRING_FROM_PAGE = 0x1C001D3C
    ROW_COUNT = 0x14001D57
    COLUMN_COUNT = 0x14001D58
    STRUCTURE_ELEMENT_CHILD_NODES = 0x24001D5F
    AUTHOR = 0x1C001D75
    LAST_MODIFIED_TIMESTAMP = 0x18001D77
    LAST_MODIFIED_TIME = 0x14001D7A
    EMBEDDED_FILE_CONTAINER = 0x20001D9B
    EMBEDDED_FILE_NAME = 0x1C001D9C
    IMAGE_FILENAME = 0x1C001DD7
    PAGE_LEVEL = 0x14001DFF
    TEXT_RUN_INDEX = 0x1C001E12
    TEXT_RUN_FORMATTING = 0x24001E13
    HYPERLINK = 0x08001E14
    HYPERLINK_PROTECTED = 0x08001E19
    WZ_HYPERLINK_URL = 0x1C001E20
    IMAGE_ALT_TEXT = 0x1C001E58
    PARAGRAPH_STYLE = 0x2000342C
    PARAGRAPH_STYLE_ID = 0x1C00345A
    NOTE_TAG_SHAPE = 0x10003464
    NOTE_TAG_LABEL = 0x1C003468
    NOTE_TAG_COMPLETED = 0x1400346F
    NOTE_TAG_DEFINITION_OID = 0x20003488
    NOTE_TAG_STATES = 0x40003489
    TEXT_EXTENDED_ASCII = 0x1C003498
    TEXT_RUN_DATA = 0x40003499
    TEXT_RUN_DATA_OBJECT = 0x24003458
    WEB_PICTURE_CONTAINER = 0x200034C8
    CHILD_GRAPH_SPACE_ELEMENT_NODES = 0x2C001D63
    META_DATA_OBJECTS_ABOVE_GRAPH_SPACE = 0x24003442


@dataclass(frozen=True, slots=True)
class PropertySet:
    """解碼後的屬性集：屬性 ID → 屬性值。"""

    values: Mapping[int, PropertyValue]

    def get(self, prop: int) -> PropertyValue:
        return self.values.get(prop)

    def has(self, prop: int) -> bool:
        return prop in self.values

    # ---- 型別化的取值輔助函式 --------------------------------------------

    def flag(self, prop: int) -> bool:
        value = self.values.get(prop)
        return bool(value) if isinstance(value, (bool, int)) else False

    def uint(self, prop: int) -> int | None:
        value = self.values.get(prop)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def float32(self, prop: int) -> float | None:
        value = self.uint(prop)
        return struct.unpack("<f", value.to_bytes(4, "little"))[0] if value is not None else None

    def raw(self, prop: int) -> bytes | None:
        value = self.values.get(prop)
        return value if isinstance(value, bytes) else None

    def text(self, prop: int) -> str | None:
        """UTF-16LE 字串屬性，去除結尾的 NUL。"""
        raw = self.raw(prop)
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00") if raw is not None else None

    def ids(self, prop: int) -> tuple[ExtendedGUID, ...]:
        value = self.values.get(prop)
        if isinstance(value, tuple) and all(isinstance(item, ExtendedGUID) for item in value):
            return value  # type: ignore[return-value]
        return ()

    def first_id(self, prop: int) -> ExtendedGUID | None:
        found = self.ids(prop)
        return found[0] if found and not found[0].is_nil else None

    def property_sets(self, prop: int) -> tuple[PropertySet, ...]:
        value = self.values.get(prop)
        if isinstance(value, PropertySet):
            return (value,)
        if isinstance(value, tuple) and all(isinstance(item, PropertySet) for item in value):
            return value  # type: ignore[return-value]
        return ()


EMPTY_PROPERTY_SET = PropertySet(MappingProxyType({}))


class _IdStream:
    """依序取出 CompactID 的串流游標。"""

    __slots__ = ("ids", "position")

    def __init__(self, ids: list[ExtendedGUID]) -> None:
        self.ids = ids
        self.position = 0

    def take(self, count: int) -> tuple[ExtendedGUID, ...]:
        if self.position + count > len(self.ids):
            raise FormatError(f"屬性集需要 {count} 個 ID，但串流只剩 {len(self.ids) - self.position} 個")
        chunk = tuple(self.ids[self.position : self.position + count])
        self.position += count
        return chunk


def _read_id_stream(r: Reader, table: GlobalIdTable) -> tuple[_IdStream, bool, bool]:
    """讀取 ObjectSpaceObjectStreamOfIDs，回傳 (串流, ExtendedStreamsPresent, OsidStreamNotPresent)。"""
    raw = r.u32()
    count = raw & 0xFFFFFF
    extended = bool((raw >> 30) & 1)
    osid_not_present = bool((raw >> 31) & 1)
    ids = [resolve_compact_id(CompactID.read(r), table) for _ in range(count)]
    return _IdStream(ids), extended, osid_not_present


def _read_property_set(r: Reader, oids: _IdStream, osids: _IdStream, contexts: _IdStream) -> PropertySet:
    count = r.u16()
    prids = [r.u32() for _ in range(count)]
    values: dict[int, PropertyValue] = {}

    for raw in prids:
        prop_type = (raw >> 26) & 0x1F
        key = raw & 0x7FFFFFFF
        match prop_type:
            case PropertyType.NO_DATA:
                value: PropertyValue = None
            case PropertyType.BOOL:
                value = bool(raw >> 31)
            case PropertyType.ONE_BYTE:
                value = r.u8()
            case PropertyType.TWO_BYTES:
                value = r.u16()
            case PropertyType.FOUR_BYTES:
                value = r.u32()
            case PropertyType.EIGHT_BYTES:
                value = r.u64()
            case PropertyType.FOUR_BYTES_LENGTH_DATA:
                value = r.bytes(r.u32())
            case PropertyType.OBJECT_ID:
                value = oids.take(1)
            case PropertyType.ARRAY_OF_OBJECT_IDS:
                value = oids.take(r.u32())
            case PropertyType.OBJECT_SPACE_ID:
                value = osids.take(1)
            case PropertyType.ARRAY_OF_OBJECT_SPACE_IDS:
                value = osids.take(r.u32())
            case PropertyType.CONTEXT_ID:
                value = contexts.take(1)
            case PropertyType.ARRAY_OF_CONTEXT_IDS:
                value = contexts.take(r.u32())
            case PropertyType.ARRAY_OF_PROPERTY_VALUES:
                element_count = r.u32()
                if element_count:
                    r.skip(4)  # 元素的 PropertyID（型別必為 PropertySet）
                value = tuple(_read_property_set(r, oids, osids, contexts) for _ in range(element_count))
            case PropertyType.PROPERTY_SET:
                value = _read_property_set(r, oids, osids, contexts)
            case _:
                raise FormatError(f"未知的屬性型別 0x{prop_type:X}（PropertyID 0x{raw:08X}）")
        values[key] = value

    return PropertySet(MappingProxyType(values))


def decode_property_set(data: bytes, entry: ObjectEntry) -> PropertySet:
    """解碼一個物件宣告所指向的屬性集。"""
    if entry.ref is None or not entry.is_property_set:
        return EMPTY_PROPERTY_SET
    r = Reader(data).sub(entry.ref.stp, entry.ref.cb)
    oids, extended, osid_not_present = _read_id_stream(r, entry.id_table)
    osids = _IdStream([])
    contexts = _IdStream([])
    if not osid_not_present:
        osids, extended, _ = _read_id_stream(r, entry.id_table)
    if extended:
        contexts, _, _ = _read_id_stream(r, entry.id_table)
    return _read_property_set(r, oids, osids, contexts)
