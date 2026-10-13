"""properties.py：屬性集解碼。

重點是物件參照串流的游標：每個參照都要依序取出下一個 ID
（pyOneNote 的 bug 是每次都取到第一個，導致整棵頁面樹無法還原）。
"""

import struct
import uuid

import pytest

from onenote_export.binary import ExtendedGUID, FileChunkReference, FormatError
from onenote_export.onestore import ObjectEntry
from onenote_export.properties import PropertyID, PropertySet, decode_property_set

GUID = uuid.UUID("11111111-2222-3333-4444-555555555555")
TABLE = {0: GUID}
JCID_PROPERTY_SET = 0x00060000 | 0x20000


def id_stream(ids: list[int], osid_not_present: bool = True, extended: bool = False) -> bytes:
    header = len(ids) | (int(extended) << 30) | (int(osid_not_present) << 31)
    return struct.pack("<I", header) + b"".join(struct.pack("<I", n) for n in ids)  # guid_index=0


def prop(prop_id: int, value: bool = False) -> int:
    return prop_id | (int(value) << 31)


def make_entry(payload: bytes, offset: int = 16) -> tuple[bytes, ObjectEntry]:
    data = b"\x00" * offset + payload
    entry = ObjectEntry(
        oid=ExtendedGUID(GUID, 1),
        jcid=JCID_PROPERTY_SET,
        ref=FileChunkReference(offset, len(payload)),
        id_table=TABLE,
    )
    return data, entry


def test_object_id_arrays_consume_stream_in_order():
    text = "哈囉".encode("utf-16-le")
    prids = [
        prop(PropertyID.ELEMENT_CHILD_NODES),  # ArrayOfObjectIDs
        prop(PropertyID.CONTENT_CHILD_NODES),  # ArrayOfObjectIDs
        prop(PropertyID.BOLD, True),  # Bool
        prop(PropertyID.RICH_EDIT_TEXT_UNICODE),  # 長度 + 資料
        prop(PropertyID.PAGE_LEVEL),  # 4 位元組
    ]
    payload = (
        id_stream([5, 6, 7])
        + struct.pack("<H", len(prids))
        + b"".join(struct.pack("<I", p) for p in prids)
        + struct.pack("<I", 2)  # ElementChildNodes 有 2 個
        + struct.pack("<I", 1)  # ContentChildNodes 有 1 個
        + struct.pack("<I", len(text))
        + text
        + struct.pack("<I", 3)
    )
    data, entry = make_entry(payload)
    props = decode_property_set(data, entry)

    assert props.ids(PropertyID.ELEMENT_CHILD_NODES) == (ExtendedGUID(GUID, 5), ExtendedGUID(GUID, 6))
    assert props.ids(PropertyID.CONTENT_CHILD_NODES) == (ExtendedGUID(GUID, 7),)
    assert props.flag(PropertyID.BOLD) is True
    assert props.text(PropertyID.RICH_EDIT_TEXT_UNICODE) == "哈囉"
    assert props.uint(PropertyID.PAGE_LEVEL) == 3


def test_nested_property_sets_share_the_id_stream():
    nested_prid = 0x40003489  # ArrayOfPropertyValues（NoteTagStates）
    inner_prid = prop(PropertyID.NOTE_TAG_DEFINITION_OID)  # ObjectID
    inner = struct.pack("<H", 1) + struct.pack("<I", inner_prid)
    payload = (
        id_stream([8, 9])
        + struct.pack("<H", 2)
        + struct.pack("<II", prop(PropertyID.PARAGRAPH_STYLE), nested_prid)
        + struct.pack("<I", 1)  # 陣列中 1 個屬性集
        + struct.pack("<I", 0x44000000)  # 元素的 PropertyID（型別 0x11）
        + inner
    )
    data, entry = make_entry(payload)
    props = decode_property_set(data, entry)
    assert props.first_id(PropertyID.PARAGRAPH_STYLE) == ExtendedGUID(GUID, 8)
    (state,) = props.property_sets(nested_prid)
    assert state.first_id(PropertyID.NOTE_TAG_DEFINITION_OID) == ExtendedGUID(GUID, 9)


def test_too_few_ids_in_stream_raises():
    payload = (
        id_stream([1]) + struct.pack("<H", 1) + struct.pack("<I", PropertyID.ELEMENT_CHILD_NODES) + struct.pack("<I", 3)
    )
    data, entry = make_entry(payload)
    with pytest.raises(FormatError, match="ID"):
        decode_property_set(data, entry)


def test_unknown_property_type_raises():
    payload = id_stream([]) + struct.pack("<H", 1) + struct.pack("<I", 0x1F << 26)
    data, entry = make_entry(payload)
    with pytest.raises(FormatError, match="未知的屬性型別"):
        decode_property_set(data, entry)


def test_entries_without_property_set_decode_to_empty():
    entry = ObjectEntry(ExtendedGUID(GUID, 1), 0x80039, None, TABLE, "<invfdo>", ".png")
    assert decode_property_set(b"", entry).values == {}


def test_typed_accessors_return_defaults_for_missing_or_wrong_types():
    props = PropertySet({1: b"\x00\x00", 2: True, 3: 0x3F800000})
    assert props.uint(1) is None
    assert props.uint(2) is None  # bool 不當成整數
    assert props.float32(3) == pytest.approx(1.0)
    assert props.ids(1) == ()
    assert props.first_id(99) is None
    assert props.text(99) is None
    assert props.flag(99) is False
    assert props.property_sets(2) == ()
