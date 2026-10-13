"""二進位讀取的基本工具。

MS-ONESTORE 檔案全部採用 little-endian。這個模組提供：

- `Reader`：在 `bytes` 上以游標讀取各種整數、GUID 等結構，越界時丟出 `FormatError`
- `ExtendedGUID`：規格中的 ExtendedGUID（GUID + 序號 n），作為物件與物件空間的識別碼
- `FileChunkReference`：指向檔案中某段資料的 (位移, 長度) 參照
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass


class FormatError(Exception):
    """檔案內容不符合 MS-ONESTORE 規格時丟出的例外。"""


NIL_GUID = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True)
class ExtendedGUID:
    """MS-ONESTORE 2.2.1 ExtendedGUID：GUID 加上 32 位元序號。"""

    guid: uuid.UUID
    n: int

    @property
    def is_nil(self) -> bool:
        return self.guid == NIL_GUID and self.n == 0

    def __str__(self) -> str:
        return f"{{{self.guid}}},{self.n}"


NIL_EXTENDED_GUID = ExtendedGUID(NIL_GUID, 0)


@dataclass(frozen=True, slots=True)
class FileChunkReference:
    """指向檔案中一段連續資料：stp 為起始位移，cb 為位元組數。"""

    stp: int
    cb: int

    @property
    def is_nil(self) -> bool:
        # 規格定義 fcrNil 為所有位元皆為 1；fcrZero 為所有位元皆為 0
        return self.cb == 0 or self.stp in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


_U8 = struct.Struct("<B")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")


class Reader:
    """以游標方式讀取 `bytes` 的輕量工具。

    所有讀取都會先檢查邊界，避免損毀檔案導致 `struct.error` 或靜默讀到錯誤資料。
    """

    __slots__ = ("data", "end", "pos")

    def __init__(self, data: bytes, pos: int = 0, end: int | None = None) -> None:
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else min(end, len(data))
        if pos < 0 or pos > self.end:
            raise FormatError(f"讀取起點 {pos} 超出範圍（長度 {self.end}）")

    # ---- 基本操作 ---------------------------------------------------------

    @property
    def remaining(self) -> int:
        return self.end - self.pos

    def sub(self, stp: int, cb: int) -> Reader:
        """建立讀取同一份資料中 [stp, stp+cb) 區段的新 Reader。"""
        if stp < 0 or cb < 0 or stp + cb > len(self.data):
            raise FormatError(f"參照區段 stp={stp} cb={cb} 超出檔案長度 {len(self.data)}")
        return Reader(self.data, stp, stp + cb)

    def bytes(self, count: int) -> bytes:
        if count < 0 or self.pos + count > self.end:
            raise FormatError(f"嘗試在位移 {self.pos} 讀取 {count} 位元組，但只剩 {self.remaining} 位元組")
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def skip(self, count: int) -> None:
        self.bytes(count)

    def _unpack(self, packer: struct.Struct) -> int:
        """以預先編譯的 Struct 直接從原始資料解碼，省去切片複製。"""
        position = self.pos
        if position + packer.size > self.end:
            raise FormatError(f"嘗試在位移 {position} 讀取 {packer.size} 位元組，但只剩 {self.remaining} 位元組")
        self.pos = position + packer.size
        return packer.unpack_from(self.data, position)[0]

    # ---- 整數 -------------------------------------------------------------

    def u8(self) -> int:
        return self._unpack(_U8)

    def u16(self) -> int:
        return self._unpack(_U16)

    def u32(self) -> int:
        return self._unpack(_U32)

    def u64(self) -> int:
        return self._unpack(_U64)

    def uint(self, size: int) -> int:
        """讀取指定位元組數（1/2/4/8）的無號整數。"""
        return int.from_bytes(self.bytes(size), "little")

    # ---- 複合結構 ---------------------------------------------------------

    def guid(self) -> uuid.UUID:
        return uuid.UUID(bytes_le=self.bytes(16))

    def extended_guid(self) -> ExtendedGUID:
        guid = self.guid()
        return ExtendedGUID(guid, self.u32())

    def fcr64x32(self) -> FileChunkReference:
        """FileChunkReference64x32：8 位元組位移 + 4 位元組長度。"""
        stp = self.u64()
        return FileChunkReference(stp, self.u32())

    def fcr32(self) -> FileChunkReference:
        stp = self.u32()
        return FileChunkReference(stp, self.u32())
