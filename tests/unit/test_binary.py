"""binary.py：Reader 與基本結構。"""

import struct
import uuid

import pytest

from onenote_export.binary import NIL_EXTENDED_GUID, ExtendedGUID, FileChunkReference, FormatError, Reader


def test_reads_little_endian_integers_in_order():
    data = struct.pack("<BHIQ", 0x12, 0x3456, 0x789ABCDE, 0x0102030405060708)
    r = Reader(data)
    assert (r.u8(), r.u16(), r.u32(), r.u64()) == (0x12, 0x3456, 0x789ABCDE, 0x0102030405060708)
    assert r.remaining == 0


def test_uint_reads_arbitrary_width():
    assert Reader(b"\x01\x02\x03\x04\x05\x06").uint(6) == 0x060504030201


def test_reading_past_end_raises_format_error():
    r = Reader(b"\x01\x02")
    with pytest.raises(FormatError):
        r.u32()


def test_sub_reader_is_bounded_to_its_range():
    r = Reader(b"\x00\x01\x02\x03\x04\x05").sub(2, 2)
    assert r.u16() == 0x0302
    with pytest.raises(FormatError):
        r.u8()


def test_sub_reader_outside_file_raises():
    with pytest.raises(FormatError):
        Reader(b"\x00" * 4).sub(2, 10)


def test_invalid_start_position_raises():
    with pytest.raises(FormatError):
        Reader(b"\x00", pos=5)


def test_guid_uses_mixed_endian_layout():
    guid = uuid.UUID("7B5C52E4-D88C-4DA7-AEB1-5378D02996D3")
    assert Reader(guid.bytes_le).guid() == guid


def test_extended_guid_and_nil():
    guid = uuid.uuid4()
    value = Reader(guid.bytes_le + struct.pack("<I", 7)).extended_guid()
    assert value == ExtendedGUID(guid, 7)
    assert not value.is_nil
    assert NIL_EXTENDED_GUID.is_nil
    assert str(value) == f"{{{guid}}},7"


def test_file_chunk_references():
    r = Reader(struct.pack("<QI", 100, 20) + struct.pack("<II", 5, 6))
    assert r.fcr64x32() == FileChunkReference(100, 20)
    assert r.fcr32() == FileChunkReference(5, 6)


@pytest.mark.parametrize(
    ("stp", "cb", "expected"),
    [(0, 0, True), (0xFFFFFFFFFFFFFFFF, 0, True), (0xFFFFFFFF, 12, True), (1024, 12, False)],
)
def test_file_chunk_reference_nil(stp, cb, expected):
    assert FileChunkReference(stp, cb).is_nil is expected
