"""onestore.py：檔頭、交易紀錄、檔案節點清單、全域 ID 表。

以手工組出的位元組驗證規格細節，包含兩個實際踩過的 bug：
- 交易紀錄片段的 nextFragment 緊接在項目陣列之後，而不是片段結尾
- 一筆交易可能跨越兩個交易紀錄片段
"""

import struct
import uuid

import pytest

from onenote_export.binary import NIL_EXTENDED_GUID, ExtendedGUID, FileChunkReference, FormatError
from onenote_export.onestore import (
    FILE_NODE_LIST_FOOTER_MAGIC,
    FILE_NODE_LIST_HEADER_MAGIC,
    GUID_FILE_FORMAT_PACKAGING,
    GUID_FILE_FORMAT_REVISION_STORE,
    GUID_FILE_TYPE_ONE,
    CompactID,
    FileNodeListReader,
    Header,
    UnsupportedFileError,
    _IdTable,
    parse_header,
    parse_transaction_log,
    resolve_compact_id,
)

# ---------------------------------------------------------------------------
# 組裝工具
# ---------------------------------------------------------------------------


def build_header(
    file_type: uuid.UUID = GUID_FILE_TYPE_ONE,
    file_format: uuid.UUID = GUID_FILE_FORMAT_REVISION_STORE,
    transactions: int = 0,
    transaction_log: tuple[int, int] = (0, 0),
    root_list: tuple[int, int] = (0, 0),
) -> bytearray:
    header = bytearray(1024)
    header[0:16] = file_type.bytes_le
    header[48:64] = file_format.bytes_le
    header[96:100] = struct.pack("<I", transactions)
    header[160:172] = struct.pack("<QI", *transaction_log)
    header[172:184] = struct.pack("<QI", *root_list)
    return header


def make_header(transactions: int, log_stp: int, log_cb: int) -> Header:
    return Header(
        guid_file_type=GUID_FILE_TYPE_ONE,
        guid_file=uuid.uuid4(),
        guid_file_format=GUID_FILE_FORMAT_REVISION_STORE,
        transactions_in_log=transactions,
        fcr_transaction_log=FileChunkReference(log_stp, log_cb),
        fcr_file_node_list_root=FileChunkReference(0, 0),
    )


def transaction_fragment(entries: list[tuple[int, int]], next_fragment: tuple[int, int], padding: int = 4) -> bytes:
    body = b"".join(struct.pack("<II", src, switch) for src, switch in entries)
    return body + struct.pack("<QI", *next_fragment) + b"\x00" * padding


def file_node(fnid: int, body: bytes = b"", base_type: int = 0) -> bytes:
    size = 4 + len(body)
    raw = fnid | (size << 10) | (base_type << 27)
    return struct.pack("<I", raw) + body


def node_list_fragment(list_id: int, sequence: int, nodes: bytes, size: int, next_fragment=(0, 0)) -> bytes:
    header = struct.pack("<QII", FILE_NODE_LIST_HEADER_MAGIC, list_id, sequence)
    body_size = size - len(header) - 20
    assert len(nodes) <= body_size
    body = nodes + b"\x00" * (body_size - len(nodes))
    return header + body + struct.pack("<QI", *next_fragment) + struct.pack("<Q", FILE_NODE_LIST_FOOTER_MAGIC)


# ---------------------------------------------------------------------------
# 檔頭
# ---------------------------------------------------------------------------


def test_parse_header_reads_core_fields():
    data = bytes(build_header(transactions=3, transaction_log=(2048, 100), root_list=(4096, 64)))
    header = parse_header(data)
    assert header.is_section and not header.is_toc
    assert header.transactions_in_log == 3
    assert header.fcr_transaction_log == FileChunkReference(2048, 100)
    assert header.fcr_file_node_list_root == FileChunkReference(4096, 64)


def test_parse_header_rejects_short_file():
    with pytest.raises(FormatError, match="1024"):
        parse_header(b"\x00" * 100)


def test_parse_header_rejects_non_onenote_file():
    with pytest.raises(FormatError, match="不是 OneNote"):
        parse_header(bytes(build_header(file_type=uuid.uuid4())))


def test_parse_header_reports_fsshttpb_packaging_as_unsupported():
    with pytest.raises(UnsupportedFileError, match="FSSHTTPB"):
        parse_header(bytes(build_header(file_format=GUID_FILE_FORMAT_PACKAGING)))


def test_parse_header_reports_unknown_format_as_unsupported():
    with pytest.raises(UnsupportedFileError):
        parse_header(bytes(build_header(file_format=uuid.uuid4())))


# ---------------------------------------------------------------------------
# 交易紀錄
# ---------------------------------------------------------------------------


def test_transaction_log_keeps_last_committed_count_per_list():
    fragment = transaction_fragment([(10, 1), (11, 2), (1, 0), (10, 3), (1, 0)], (0, 0))
    data = b"\x00" * 64 + fragment
    counts = parse_transaction_log(data, make_header(2, 64, len(fragment)))
    assert counts == {10: 3, 11: 2}


def test_transaction_log_ignores_uncommitted_transactions():
    fragment = transaction_fragment([(10, 1), (1, 0), (10, 9), (1, 0)], (0, 0))
    data = b"\x00" * 64 + fragment
    counts = parse_transaction_log(data, make_header(1, 64, len(fragment)))
    assert counts == {10: 1}


def test_transaction_log_follows_next_fragment_located_right_after_entries():
    """回歸測試：片段結尾有 4 位元組填充時，nextFragment 不在 cb-12 的位置。"""
    second = transaction_fragment([(20, 5), (1, 0)], (0, 0))
    second_stp = 512
    first = transaction_fragment([(10, 1), (1, 0)], (second_stp, len(second)), padding=4)
    data = bytearray(1024)
    data[64 : 64 + len(first)] = first
    data[second_stp : second_stp + len(second)] = second
    counts = parse_transaction_log(bytes(data), make_header(2, 64, len(first)))
    assert counts == {10: 1, 20: 5}


def test_transaction_spanning_two_fragments_is_applied():
    """回歸測試：交易的項目在第一個片段、結束哨兵在第二個片段。"""
    second = transaction_fragment([(1, 0)], (0, 0))
    second_stp = 512
    first = transaction_fragment([(10, 1), (1, 0), (10, 2)], (second_stp, len(second)))
    data = bytearray(1024)
    data[64 : 64 + len(first)] = first
    data[second_stp : second_stp + len(second)] = second
    counts = parse_transaction_log(bytes(data), make_header(2, 64, len(first)))
    assert counts == {10: 2}


def test_transaction_log_stops_on_fragment_cycle():
    fragment_size = len(transaction_fragment([(10, 1)], (0, 0)))
    fragment = transaction_fragment([(10, 1)], (64, fragment_size))  # 指回自己
    data = b"\x00" * 64 + fragment
    counts = parse_transaction_log(data, make_header(5, 64, len(fragment)))
    assert counts == {}


# ---------------------------------------------------------------------------
# 檔案節點清單
# ---------------------------------------------------------------------------


def test_file_node_list_reads_nodes_until_committed_count():
    nodes = file_node(0x004, b"A" * 20) + file_node(0x00C, b"B" * 20) + file_node(0x01C)
    fragment = node_list_fragment(list_id=7, sequence=0, nodes=nodes, size=128)
    data = b"\x00" * 64 + fragment
    reader = FileNodeListReader(data, {7: 2})
    result = reader.read(FileChunkReference(64, len(fragment)))
    assert [node.fnid for node in result] == [0x004, 0x00C]
    assert result[0].body.bytes(20) == b"A" * 20


def test_file_node_list_follows_fragments_and_stops_at_terminator():
    second = node_list_fragment(7, 1, file_node(0x01C) + file_node(0x0FF), size=64)
    first = node_list_fragment(7, 0, file_node(0x004, b"x" * 20) + file_node(0x0FF), size=96, next_fragment=(256, 64))
    data = bytearray(512)
    data[64 : 64 + len(first)] = first
    data[256 : 256 + len(second)] = second
    result = FileNodeListReader(bytes(data), {}).read(FileChunkReference(64, len(first)))
    assert [node.fnid for node in result] == [0x004, 0x01C]


def test_file_node_list_reads_chunk_reference_for_base_type_1():
    body = struct.pack("<QI", 4096, 256) + b"payload!"
    fragment = node_list_fragment(3, 0, file_node(0x0A4, body, base_type=1), size=96)
    data = b"\x00" * 64 + fragment
    (node,) = FileNodeListReader(data, {3: 1}).read(FileChunkReference(64, len(fragment)))
    assert node.ref == FileChunkReference(4096, 256)
    assert node.body.bytes(8) == b"payload!"


def test_file_node_list_rejects_bad_magic():
    data = b"\x00" * 64 + b"\x00" * 64
    with pytest.raises(FormatError, match="magic"):
        FileNodeListReader(data, {}).read(FileChunkReference(64, 64))


def test_file_node_list_stops_on_mismatched_sequence():
    second = node_list_fragment(7, 5, file_node(0x01C), size=64)  # 序號應為 1
    first = node_list_fragment(7, 0, file_node(0x004, b"x" * 20) + file_node(0x0FF), size=96, next_fragment=(256, 64))
    data = bytearray(512)
    data[64 : 64 + len(first)] = first
    data[256 : 256 + len(second)] = second
    result = FileNodeListReader(bytes(data), {}).read(FileChunkReference(64, len(first)))
    assert [node.fnid for node in result] == [0x004]


# ---------------------------------------------------------------------------
# CompactID 與全域 ID 表
# ---------------------------------------------------------------------------


def test_compact_id_resolution():
    guid = uuid.uuid4()
    cid = CompactID(n=5, guid_index=3)
    assert resolve_compact_id(cid, {3: guid}) == ExtendedGUID(guid, 5)
    assert resolve_compact_id(CompactID(0, 0), {}) == NIL_EXTENDED_GUID


def test_compact_id_with_unknown_index_raises():
    with pytest.raises(FormatError, match="GUID 索引"):
        resolve_compact_id(CompactID(1, 9), {})


def test_id_table_snapshot_is_isolated_from_later_changes():
    table = _IdTable()
    first_guid = uuid.uuid4()
    table.set(0, first_guid)
    snapshot = table.snapshot()
    assert table.snapshot() is snapshot  # 未變動時重複使用同一份快照

    table.clear()
    table.set(0, uuid.uuid4())
    assert snapshot[0] == first_guid
    assert table.snapshot() is not snapshot
