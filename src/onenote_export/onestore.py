"""MS-ONESTORE（OneNote revision store 檔案格式）解析器。

OneNote 2010 之後的桌面版 `.one`（節）與 `.onetoc2`（目錄）檔案都採用這種格式。
本模組只負責「儲存層」，把檔案還原成：

    檔案
     └─ 物件空間（object space，一個節本身 + 每一頁各一個）
         └─ 目前生效的修訂版（revision）
             ├─ 物件表：ExtendedGUID → 物件宣告（JCID、屬性集位置…）
             └─ 根物件：角色（role）→ ExtendedGUID

物件的屬性內容由 `properties.py` 解碼，頁面語意（大綱、段落、表格…）由 `document.py` 處理。

參考規格：[MS-ONESTORE] Open Specifications, Microsoft。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .binary import NIL_EXTENDED_GUID, ExtendedGUID, FileChunkReference, FormatError, Reader

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 檔案類型與格式 GUID（[MS-ONESTORE] 2.3.1）
# ---------------------------------------------------------------------------

GUID_FILE_TYPE_ONE = uuid.UUID("7B5C52E4-D88C-4DA7-AEB1-5378D02996D3")
GUID_FILE_TYPE_ONETOC2 = uuid.UUID("43FF2FA1-EFD9-4C76-9EE2-10EA5722765F")
GUID_FILE_FORMAT_REVISION_STORE = uuid.UUID("109ADD3F-911B-49F5-A5D0-1791EDC8AED8")
# OneDrive / Office 365 下載的 .one 使用 FSSHTTPB 封裝格式，本工具不支援
GUID_FILE_FORMAT_PACKAGING = uuid.UUID("638DE92F-A6D4-4BC1-9A36-B3FC2511A5B7")

FILE_NODE_LIST_HEADER_MAGIC = 0xA4567AB1F5F7F4C4
FILE_NODE_LIST_FOOTER_MAGIC = 0x8BC215C38233BA4B
FILE_DATA_STORE_HEADER_GUID = uuid.UUID("BDE316E7-2665-4511-A4C4-8D4D0B7A9EAC")

# 修訂版角色（[MS-ONESTORE] 2.1.8）
ROLE_DEFAULT_CONTENT = 0x1
ROLE_METADATA = 0x2

# 避免惡意或損毀檔案造成無窮迴圈
MAX_FRAGMENTS = 100_000


class FileNodeID:
    """會用到的 FileNode 類型代碼（[MS-ONESTORE] 2.4.3）。"""

    OBJECT_SPACE_MANIFEST_ROOT = 0x004
    OBJECT_SPACE_MANIFEST_LIST_REFERENCE = 0x008
    OBJECT_SPACE_MANIFEST_LIST_START = 0x00C
    REVISION_MANIFEST_LIST_REFERENCE = 0x010
    REVISION_MANIFEST_LIST_START = 0x014
    REVISION_MANIFEST_START4 = 0x01B
    REVISION_MANIFEST_END = 0x01C
    REVISION_MANIFEST_START6 = 0x01E
    REVISION_MANIFEST_START7 = 0x01F
    GLOBAL_ID_TABLE_START = 0x021
    GLOBAL_ID_TABLE_START2 = 0x022
    GLOBAL_ID_TABLE_ENTRY = 0x024
    GLOBAL_ID_TABLE_ENTRY2 = 0x025
    GLOBAL_ID_TABLE_ENTRY3 = 0x026
    GLOBAL_ID_TABLE_END = 0x028
    OBJECT_DECLARATION_WITH_REF_COUNT = 0x02D
    OBJECT_DECLARATION_WITH_REF_COUNT2 = 0x02E
    OBJECT_REVISION_WITH_REF_COUNT = 0x041
    OBJECT_REVISION_WITH_REF_COUNT2 = 0x042
    ROOT_OBJECT_REFERENCE2 = 0x059
    ROOT_OBJECT_REFERENCE3 = 0x05A
    REVISION_ROLE_DECLARATION = 0x05C
    REVISION_ROLE_AND_CONTEXT_DECLARATION = 0x05D
    OBJECT_DECLARATION_FILE_DATA3_REF_COUNT = 0x072
    OBJECT_DECLARATION_FILE_DATA3_LARGE_REF_COUNT = 0x073
    OBJECT_DATA_ENCRYPTION_KEY_V2 = 0x07C
    FILE_DATA_STORE_LIST_REFERENCE = 0x090
    FILE_DATA_STORE_OBJECT_REFERENCE = 0x094
    OBJECT_DECLARATION2_REF_COUNT = 0x0A4
    OBJECT_DECLARATION2_LARGE_REF_COUNT = 0x0A5
    OBJECT_GROUP_LIST_REFERENCE = 0x0B0
    OBJECT_GROUP_START = 0x0B4
    OBJECT_GROUP_END = 0x0B8
    READ_ONLY_OBJECT_DECLARATION2_REF_COUNT = 0x0C4
    READ_ONLY_OBJECT_DECLARATION2_LARGE_REF_COUNT = 0x0C5
    CHUNK_TERMINATOR = 0x0FF


class UnsupportedFileError(FormatError):
    """檔案可辨識，但屬於本工具不支援的變體（例如 FSSHTTPB 封裝、加密節）。"""


# ---------------------------------------------------------------------------
# 檔頭
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Header:
    """1024 位元組檔頭中本工具需要的欄位（[MS-ONESTORE] 2.3.1）。"""

    guid_file_type: uuid.UUID
    guid_file: uuid.UUID
    guid_file_format: uuid.UUID
    transactions_in_log: int
    fcr_transaction_log: FileChunkReference
    fcr_file_node_list_root: FileChunkReference

    @property
    def is_section(self) -> bool:
        return self.guid_file_type == GUID_FILE_TYPE_ONE

    @property
    def is_toc(self) -> bool:
        return self.guid_file_type == GUID_FILE_TYPE_ONETOC2


def parse_header(data: bytes) -> Header:
    if len(data) < 1024:
        raise FormatError(f"檔案長度只有 {len(data)} 位元組，不足 1024 位元組的檔頭")
    r = Reader(data)
    guid_file_type = r.guid()
    guid_file = r.guid()
    r.skip(16)  # guidLegacyFileVersion
    guid_file_format = r.guid()

    if guid_file_type not in (GUID_FILE_TYPE_ONE, GUID_FILE_TYPE_ONETOC2):
        raise FormatError(f"不是 OneNote 檔案（guidFileType={guid_file_type}）")
    if guid_file_format == GUID_FILE_FORMAT_PACKAGING:
        raise UnsupportedFileError(
            "這是 OneDrive／Office 365 下載的 FSSHTTPB 封裝格式，目前只支援桌面版 revision store 格式"
        )
    if guid_file_format != GUID_FILE_FORMAT_REVISION_STORE:
        raise UnsupportedFileError(f"不支援的檔案格式 guidFileFormat={guid_file_format}")

    r.pos = 96
    transactions_in_log = r.u32()
    r.pos = 160
    fcr_transaction_log = r.fcr64x32()
    fcr_file_node_list_root = r.fcr64x32()
    return Header(
        guid_file_type=guid_file_type,
        guid_file=guid_file,
        guid_file_format=guid_file_format,
        transactions_in_log=transactions_in_log,
        fcr_transaction_log=fcr_transaction_log,
        fcr_file_node_list_root=fcr_file_node_list_root,
    )


# ---------------------------------------------------------------------------
# 交易紀錄（決定每個檔案節點清單中「已提交」的節點數）
# ---------------------------------------------------------------------------


def parse_transaction_log(data: bytes, header: Header) -> dict[int, int]:
    """回傳 {FileNodeListID: 已提交的 FileNode 數量}。

    檔案節點清單的片段中可能殘留尚未提交（寫到一半）的節點，
    必須以交易紀錄中的數量為準，否則會讀到垃圾資料（[MS-ONESTORE] 2.3.3）。
    """
    counts: dict[int, int] = {}
    remaining_transactions = header.transactions_in_log
    fcr = header.fcr_transaction_log
    root = Reader(data)
    visited: set[int] = set()
    # 一筆交易可能跨越多個片段，所以暫存區放在迴圈外
    pending: dict[int, int] = {}

    while remaining_transactions > 0 and not fcr.is_nil:
        if fcr.stp in visited:
            log.warning("交易紀錄片段出現循環參照，停止讀取")
            break
        visited.add(fcr.stp)
        fragment = root.sub(fcr.stp, fcr.cb)
        entry_count = (fcr.cb - 12) // 8
        for _ in range(entry_count):
            src_id = fragment.u32()
            switch = fragment.u32()
            if src_id == 0x00000001:
                # 哨兵：一筆交易結束，套用這筆交易中的變更
                counts.update(pending)
                pending.clear()
                remaining_transactions -= 1
                if remaining_transactions == 0:
                    break
            elif src_id != 0:
                pending[src_id] = switch
        # nextFragment 緊接在項目陣列之後（片段結尾可能有對齊用的填充位元組）
        fragment.pos = fcr.stp + entry_count * 8
        fcr = fragment.fcr64x32()

    if remaining_transactions > 0:
        log.warning("交易紀錄不完整：尚有 %d 筆交易未讀到", remaining_transactions)
    return counts


# ---------------------------------------------------------------------------
# 檔案節點清單
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FileNode:
    """一個 FileNode。

    `ref`：BaseType 1／2 時，節點本體開頭的 FileNodeChunkReference
    `body`：定位在 ref 之後（若有）的節點本體讀取器
    """

    fnid: int
    base_type: int
    ref: FileChunkReference | None
    body: Reader


def _read_node_chunk_reference(r: Reader, stp_format: int, cb_format: int) -> FileChunkReference:
    """依 StpFormat／CbFormat 讀取壓縮過的 FileNodeChunkReference（[MS-ONESTORE] 2.2.4.2）。"""
    match stp_format:
        case 0:
            stp = r.u64()
        case 1:
            stp = r.u32()
        case 2:
            stp = r.u16() * 8
        case _:
            stp = r.u32() * 8
    match cb_format:
        case 0:
            cb = r.u32()
        case 1:
            cb = r.u64()
        case 2:
            cb = r.u8() * 8
        case _:
            cb = r.u16() * 8
    return FileChunkReference(stp, cb)


class FileNodeListReader:
    """讀取檔案節點清單（跨多個片段），並遵守交易紀錄中的已提交數量。"""

    def __init__(self, data: bytes, committed_counts: Mapping[int, int]) -> None:
        self._root = Reader(data)
        self._committed = committed_counts

    def read(self, fcr: FileChunkReference) -> list[FileNode]:
        return list(self._iter_nodes(fcr))

    def _iter_nodes(self, fcr: FileChunkReference) -> Iterator[FileNode]:
        list_id: int | None = None
        expected_sequence = 0
        limit: int | None = None
        emitted = 0
        visited: set[int] = set()

        while not fcr.is_nil:
            if fcr.stp in visited or len(visited) > MAX_FRAGMENTS:
                log.warning("檔案節點清單片段循環參照（位移 %d），停止讀取", fcr.stp)
                return
            visited.add(fcr.stp)
            if fcr.cb < 36:
                raise FormatError(f"檔案節點清單片段過小（cb={fcr.cb}）")

            fragment = self._root.sub(fcr.stp, fcr.cb)
            if fragment.u64() != FILE_NODE_LIST_HEADER_MAGIC:
                raise FormatError(f"位移 {fcr.stp} 不是檔案節點清單片段（magic 不符）")
            fragment_list_id = fragment.u32()
            sequence = fragment.u32()
            if list_id is None:
                list_id = fragment_list_id
                limit = self._committed.get(list_id)
            elif fragment_list_id != list_id or sequence != expected_sequence:
                log.warning("片段清單 ID 或序號不一致（清單 %s），停止讀取", list_id)
                return
            expected_sequence = sequence + 1

            nodes_end = fcr.stp + fcr.cb - 20
            while fragment.pos + 4 <= nodes_end:
                if limit is not None and emitted >= limit:
                    return
                node_start = fragment.pos
                raw = fragment.u32()
                fnid = raw & 0x3FF
                size = (raw >> 10) & 0x1FFF
                if raw == 0 or fnid == FileNodeID.CHUNK_TERMINATOR:
                    break
                if size < 4 or node_start + size > nodes_end:
                    log.warning("FileNode 大小異常（fnid=0x%03X size=%d），停止讀取此片段", fnid, size)
                    break
                stp_format = (raw >> 23) & 0x3
                cb_format = (raw >> 25) & 0x3
                base_type = (raw >> 27) & 0xF
                body = self._root.sub(node_start + 4, size - 4)
                ref = _read_node_chunk_reference(body, stp_format, cb_format) if base_type in (1, 2) else None
                yield FileNode(fnid, base_type, ref, body)
                emitted += 1
                fragment.pos = node_start + size

            fragment.pos = nodes_end
            fcr = fragment.fcr64x32()
            if fragment.u64() != FILE_NODE_LIST_FOOTER_MAGIC:
                log.warning("檔案節點清單片段 footer 不符（位移 %d）", fcr.stp)
            if limit is not None and emitted >= limit:
                return


# ---------------------------------------------------------------------------
# 物件與修訂版
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompactID:
    """CompactID：以全域 ID 表的索引壓縮表示的 ExtendedGUID。"""

    n: int
    guid_index: int

    @classmethod
    def read(cls, r: Reader) -> CompactID:
        raw = r.u32()
        return cls(raw & 0xFF, raw >> 8)

    @property
    def is_nil(self) -> bool:
        return self.n == 0 and self.guid_index == 0


GlobalIdTable = Mapping[int, uuid.UUID]


def resolve_compact_id(cid: CompactID, table: GlobalIdTable) -> ExtendedGUID:
    if cid.is_nil:
        return NIL_EXTENDED_GUID
    guid = table.get(cid.guid_index)
    if guid is None:
        raise FormatError(f"CompactID 的 GUID 索引 {cid.guid_index} 不在全域 ID 表中")
    return ExtendedGUID(guid, cid.n)


@dataclass(frozen=True, slots=True)
class ObjectEntry:
    """一個物件宣告。

    - `ref`：屬性集（ObjectSpaceObjectPropSet）在檔案中的位置；檔案資料物件則為 None
    - `id_table`：解析屬性集內 CompactID 時要用的全域 ID 表
    - `file_data_reference`／`extension`：檔案資料物件（圖片、附件）專用
    """

    oid: ExtendedGUID
    jcid: int
    ref: FileChunkReference | None
    id_table: GlobalIdTable
    file_data_reference: str | None = None
    extension: str | None = None

    @property
    def is_property_set(self) -> bool:
        return bool(self.jcid & 0x20000)


class _IdTable:
    """建構中的全域 ID 表。

    物件宣告需要保存「宣告當下」的 ID 表；快照只在內容變動後才重建，
    避免每個物件宣告都複製一次整張表。
    """

    __slots__ = ("_entries", "_snapshot")

    def __init__(self) -> None:
        self._entries: dict[int, uuid.UUID] = {}
        self._snapshot: GlobalIdTable | None = None

    def clear(self) -> None:
        self._entries = {}
        self._snapshot = None

    def set(self, index: int, guid: uuid.UUID) -> None:
        self._entries[index] = guid
        self._snapshot = None

    def snapshot(self) -> GlobalIdTable:
        if self._snapshot is None:
            self._snapshot = MappingProxyType(dict(self._entries))
        return self._snapshot


@dataclass(slots=True)
class Revision:
    rid: ExtendedGUID
    rid_dependent: ExtendedGUID
    role: int
    context: ExtendedGUID
    objects: dict[ExtendedGUID, ObjectEntry] = field(default_factory=dict)
    roots: dict[int, ExtendedGUID] = field(default_factory=dict)
    id_table: _IdTable = field(default_factory=_IdTable)


@dataclass(frozen=True, slots=True)
class ObjectSpace:
    """一個物件空間在目前修訂版下的完整內容（已合併相依修訂版）。"""

    gosid: ExtendedGUID
    objects: Mapping[ExtendedGUID, ObjectEntry]
    roots: Mapping[int, ExtendedGUID]

    def root(self, role: int = ROLE_DEFAULT_CONTENT) -> ObjectEntry | None:
        oid = self.roots.get(role)
        return self.objects.get(oid) if oid is not None else None


def _read_string_in_storage_buffer(r: Reader) -> str:
    cch = r.u32()
    return r.bytes(cch * 2).decode("utf-16-le", errors="replace")


class _RevisionManifestParser:
    """解析一個物件空間的修訂版清單，並組出目前生效的修訂版內容。"""

    def __init__(self, lists: FileNodeListReader) -> None:
        self._lists = lists

    def parse(self, gosid: ExtendedGUID, fcr: FileChunkReference) -> ObjectSpace:
        revisions: dict[ExtendedGUID, Revision] = {}
        assignments: dict[tuple[ExtendedGUID, int], ExtendedGUID] = {}
        order: list[ExtendedGUID] = []
        current: Revision | None = None

        for node in self._lists.read(fcr):
            match node.fnid:
                case FileNodeID.REVISION_MANIFEST_START4:
                    rid = node.body.extended_guid()
                    dependent = node.body.extended_guid()
                    node.body.skip(8)  # timeCreation
                    current = Revision(rid, dependent, node.body.u32(), NIL_EXTENDED_GUID)
                case FileNodeID.REVISION_MANIFEST_START6 | FileNodeID.REVISION_MANIFEST_START7:
                    rid = node.body.extended_guid()
                    dependent = node.body.extended_guid()
                    role = node.body.u32()
                    node.body.skip(2)  # odcsDefault
                    context = (
                        node.body.extended_guid()
                        if node.fnid == FileNodeID.REVISION_MANIFEST_START7
                        else NIL_EXTENDED_GUID
                    )
                    current = Revision(rid, dependent, role, context)
                case FileNodeID.REVISION_MANIFEST_END:
                    if current is not None:
                        revisions[current.rid] = current
                        order.append(current.rid)
                        assignments[(current.context, current.role)] = current.rid
                    current = None
                case FileNodeID.REVISION_ROLE_DECLARATION:
                    rid = node.body.extended_guid()
                    assignments[(NIL_EXTENDED_GUID, node.body.u32())] = rid
                case FileNodeID.REVISION_ROLE_AND_CONTEXT_DECLARATION:
                    rid = node.body.extended_guid()
                    role = node.body.u32()
                    assignments[(node.body.extended_guid(), role)] = rid
                case FileNodeID.OBJECT_DATA_ENCRYPTION_KEY_V2:
                    raise UnsupportedFileError("這個節有密碼保護（已加密），無法匯出")
                case _ if current is not None:
                    self._handle_revision_node(node, current, revisions)

        if not order:
            return ObjectSpace(gosid, MappingProxyType({}), MappingProxyType({}))

        content_rid = assignments.get((NIL_EXTENDED_GUID, ROLE_DEFAULT_CONTENT), order[-1])
        objects, roots = self._flatten(content_rid, revisions)
        # 物件空間的中繼資料（例如頁面標題快取）可能放在另一個角色的修訂版
        for (context, role), rid in assignments.items():
            if context.is_nil and role != ROLE_DEFAULT_CONTENT and rid != content_rid:
                extra_objects, extra_roots = self._flatten(rid, revisions)
                for key, entry in extra_objects.items():
                    objects.setdefault(key, entry)
                for key, oid in extra_roots.items():
                    roots.setdefault(key, oid)
        return ObjectSpace(gosid, MappingProxyType(objects), MappingProxyType(roots))

    @staticmethod
    def _flatten(
        rid: ExtendedGUID, revisions: Mapping[ExtendedGUID, Revision]
    ) -> tuple[dict[ExtendedGUID, ObjectEntry], dict[int, ExtendedGUID]]:
        """沿著 ridDependent 鏈合併物件與根物件；較新的修訂版覆蓋較舊的。"""
        chain: list[Revision] = []
        seen: set[ExtendedGUID] = set()
        cursor = revisions.get(rid)
        while cursor is not None and cursor.rid not in seen:
            seen.add(cursor.rid)
            chain.append(cursor)
            cursor = None if cursor.rid_dependent.is_nil else revisions.get(cursor.rid_dependent)

        objects: dict[ExtendedGUID, ObjectEntry] = {}
        roots: dict[int, ExtendedGUID] = {}
        for revision in reversed(chain):
            objects.update(revision.objects)
            roots.update(revision.roots)
        return objects, roots

    def _handle_revision_node(
        self, node: FileNode, revision: Revision, revisions: Mapping[ExtendedGUID, Revision]
    ) -> None:
        match node.fnid:
            case FileNodeID.OBJECT_GROUP_LIST_REFERENCE:
                assert node.ref is not None
                self._parse_object_group(node.ref, revision)
            case FileNodeID.ROOT_OBJECT_REFERENCE2:
                cid = CompactID.read(node.body)
                revision.roots[node.body.u32()] = resolve_compact_id(cid, revision.id_table.snapshot())
            case FileNodeID.ROOT_OBJECT_REFERENCE3:
                oid = node.body.extended_guid()
                revision.roots[node.body.u32()] = oid
            case _:
                dependency = revisions.get(revision.rid_dependent)
                dependency_table = dependency.id_table.snapshot() if dependency is not None else {}
                handled = _handle_id_table_node(node, revision.id_table, dependency_table)
                if not handled:
                    _handle_declaration_node(node, revision.objects, revision.id_table, revisions)

    def _parse_object_group(self, fcr: FileChunkReference, revision: Revision) -> None:
        """物件群組有自己的全域 ID 表，群組內的物件宣告以它解析 CompactID。"""
        table = _IdTable()
        for node in self._lists.read(fcr):
            if node.fnid in (FileNodeID.OBJECT_GROUP_START, FileNodeID.OBJECT_GROUP_END):
                continue
            if not _handle_id_table_node(node, table, {}):
                _handle_declaration_node(node, revision.objects, table, {})


def _handle_id_table_node(node: FileNode, table: _IdTable, dependency_table: GlobalIdTable) -> bool:
    """處理全域 ID 表相關節點；回傳是否已處理。"""
    match node.fnid:
        case FileNodeID.GLOBAL_ID_TABLE_START | FileNodeID.GLOBAL_ID_TABLE_START2:
            table.clear()
        case FileNodeID.GLOBAL_ID_TABLE_ENTRY:
            index = node.body.u32()
            table.set(index, node.body.guid())
        case FileNodeID.GLOBAL_ID_TABLE_ENTRY2:
            index_from = node.body.u32()
            index_to = node.body.u32()
            if index_from in dependency_table:
                table.set(index_to, dependency_table[index_from])
        case FileNodeID.GLOBAL_ID_TABLE_ENTRY3:
            copy_from = node.body.u32()
            count = node.body.u32()
            copy_to = node.body.u32()
            for offset in range(count):
                if copy_from + offset in dependency_table:
                    table.set(copy_to + offset, dependency_table[copy_from + offset])
        case FileNodeID.GLOBAL_ID_TABLE_END:
            pass
        case _:
            return False
    return True


def _handle_declaration_node(
    node: FileNode,
    objects: dict[ExtendedGUID, ObjectEntry],
    table: _IdTable,
    revisions: Mapping[ExtendedGUID, Revision],
) -> None:
    """處理物件宣告相關節點，把結果寫入 `objects`。"""
    # 以快照保存宣告當下的 ID 表，之後的 GlobalIdTableStart 不會影響它
    snapshot = table.snapshot()
    body = node.body
    match node.fnid:
        case (
            FileNodeID.OBJECT_DECLARATION2_REF_COUNT
            | FileNodeID.OBJECT_DECLARATION2_LARGE_REF_COUNT
            | FileNodeID.READ_ONLY_OBJECT_DECLARATION2_REF_COUNT
            | FileNodeID.READ_ONLY_OBJECT_DECLARATION2_LARGE_REF_COUNT
        ):
            oid = resolve_compact_id(CompactID.read(body), snapshot)
            jcid = body.u32()
            objects[oid] = ObjectEntry(oid, jcid, node.ref, snapshot)
        case FileNodeID.OBJECT_DECLARATION_WITH_REF_COUNT | FileNodeID.OBJECT_DECLARATION_WITH_REF_COUNT2:
            oid = resolve_compact_id(CompactID.read(body), snapshot)
            bits = body.uint(6)
            jci = bits & 0x3FF
            objects[oid] = ObjectEntry(oid, 0x20000 | jci, node.ref, snapshot)
        case FileNodeID.OBJECT_REVISION_WITH_REF_COUNT | FileNodeID.OBJECT_REVISION_WITH_REF_COUNT2:
            # 只更新既有物件的資料，JCID 沿用先前的宣告
            oid = resolve_compact_id(CompactID.read(body), snapshot)
            previous = objects.get(oid) or _find_in_revisions(oid, revisions)
            if previous is None:
                log.debug("ObjectRevision 參照到未宣告的物件 %s，略過", oid)
                return
            objects[oid] = ObjectEntry(oid, previous.jcid, node.ref, snapshot)
        case (
            FileNodeID.OBJECT_DECLARATION_FILE_DATA3_REF_COUNT
            | FileNodeID.OBJECT_DECLARATION_FILE_DATA3_LARGE_REF_COUNT
        ):
            oid = resolve_compact_id(CompactID.read(body), snapshot)
            jcid = body.u32()
            body.skip(1 if node.fnid == FileNodeID.OBJECT_DECLARATION_FILE_DATA3_REF_COUNT else 4)
            reference = _read_string_in_storage_buffer(body)
            extension = _read_string_in_storage_buffer(body)
            objects[oid] = ObjectEntry(oid, jcid, None, snapshot, reference, extension)
        case _:
            # 其餘節點（參照計數覆寫、資料簽章群組等）與內容無關
            pass


def _find_in_revisions(oid: ExtendedGUID, revisions: Mapping[ExtendedGUID, Revision]) -> ObjectEntry | None:
    for revision in revisions.values():
        if oid in revision.objects:
            return revision.objects[oid]
    return None


# ---------------------------------------------------------------------------
# 整個檔案
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OneStoreFile:
    """解析完成的 revision store 檔案。"""

    data: bytes
    header: Header
    root_gosid: ExtendedGUID
    object_spaces: Mapping[ExtendedGUID, ObjectSpace]
    file_data_store: Mapping[uuid.UUID, FileChunkReference]

    @property
    def root_space(self) -> ObjectSpace:
        space = self.object_spaces.get(self.root_gosid)
        if space is None:
            raise FormatError("找不到根物件空間")
        return space

    def read_file_data(self, reference: str | None) -> bytes | None:
        """依 FileDataReference 字串取出內嵌檔案的位元組內容。

        - `<ifndf>{GUID}`：存在檔案內的 FileDataStoreObject
        - `<file>名稱`：存在筆記本旁的 onefiles 資料夾（備份中通常不存在），回傳 None
        - `<invfdo>`：無效參照，回傳 None
        """
        if not reference or not reference.startswith("<ifndf>"):
            return None
        try:
            guid = uuid.UUID(reference.removeprefix("<ifndf>").strip("{}\x00 "))
        except ValueError:
            log.warning("無法解析檔案資料參照 %r", reference)
            return None
        fcr = self.file_data_store.get(guid)
        if fcr is None:
            log.warning("找不到檔案資料 %s", guid)
            return None
        r = Reader(self.data).sub(fcr.stp, fcr.cb)
        if r.guid() != FILE_DATA_STORE_HEADER_GUID:
            log.warning("檔案資料 %s 的 header GUID 不符", guid)
            return None
        length = r.u64()
        r.skip(12)  # unused + reserved
        return r.bytes(length)


def parse_onestore(data: bytes) -> OneStoreFile:
    """解析整個 revision store 檔案。"""
    header = parse_header(data)
    committed = parse_transaction_log(data, header)
    lists = FileNodeListReader(data, committed)
    revision_parser = _RevisionManifestParser(lists)

    root_gosid = NIL_EXTENDED_GUID
    object_spaces: dict[ExtendedGUID, ObjectSpace] = {}
    file_data_store: dict[uuid.UUID, FileChunkReference] = {}

    for node in lists.read(header.fcr_file_node_list_root):
        match node.fnid:
            case FileNodeID.OBJECT_SPACE_MANIFEST_ROOT:
                root_gosid = node.body.extended_guid()
            case FileNodeID.OBJECT_SPACE_MANIFEST_LIST_REFERENCE:
                assert node.ref is not None
                gosid = node.body.extended_guid()
                try:
                    object_spaces[gosid] = _parse_object_space(lists, revision_parser, gosid, node.ref)
                except UnsupportedFileError:
                    raise
                except FormatError as error:
                    log.warning("物件空間 %s 解析失敗，略過：%s", gosid, error)
            case FileNodeID.FILE_DATA_STORE_LIST_REFERENCE:
                assert node.ref is not None
                for item in lists.read(node.ref):
                    if item.fnid == FileNodeID.FILE_DATA_STORE_OBJECT_REFERENCE and item.ref is not None:
                        file_data_store[item.body.guid()] = item.ref

    return OneStoreFile(
        data=data,
        header=header,
        root_gosid=root_gosid,
        object_spaces=MappingProxyType(object_spaces),
        file_data_store=MappingProxyType(file_data_store),
    )


def _parse_object_space(
    lists: FileNodeListReader,
    revision_parser: _RevisionManifestParser,
    gosid: ExtendedGUID,
    fcr: FileChunkReference,
) -> ObjectSpace:
    # 物件空間清單可能有多個修訂版清單參照，以最後一個為準
    revision_list: FileChunkReference | None = None
    for node in lists.read(fcr):
        if node.fnid == FileNodeID.REVISION_MANIFEST_LIST_REFERENCE:
            revision_list = node.ref
    if revision_list is None:
        return ObjectSpace(gosid, MappingProxyType({}), MappingProxyType({}))
    return revision_parser.parse(gosid, revision_list)
