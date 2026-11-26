"""Read native SWG data tables, template CRC catalogs and IFF block positions."""

import struct


def blocks(data):
    offset = 0
    while offset < len(data):
        tag, size = struct.unpack_from(">4sI", data, offset)
        end = offset + 8 + size
        if end > len(data):
            raise ValueError("Truncated IFF block")
        yield tag, data[offset + 8:end]
        offset = end


def read_iff_table(path):
    outer = list(blocks(path.read_bytes()))
    assert len(outer) == 1 and outer[0][0] == b"FORM"
    assert outer[0][1][:4] == b"DTII"
    version = list(blocks(outer[0][1][4:]))
    assert len(version) == 1 and version[0][1][:4] == b"0001"
    chunks = dict(blocks(version[0][1][4:]))
    columns = chunks[b"COLS"][4:].rstrip(b"\0").decode().split("\0")
    assert len(columns) == struct.unpack_from("<I", chunks[b"COLS"])[0]
    types = chunks[b"TYPE"].rstrip(b"\0").decode().split("\0")
    assert len(columns) == len(types)
    data = chunks[b"ROWS"]
    count = struct.unpack_from("<I", data)[0]
    offset = 4
    rows = []
    for _ in range(count):
        row = {}
        for column, kind in zip(columns, types):
            if kind[0] in "scp":
                end = data.index(0, offset)
                value = data[offset:end].decode()
                offset = end + 1
            else:
                # Hash-string columns store a 32-bit CRC (DT_HashString's
                # basic type is DT_Int), as used by server buildout tables.
                # Enums also use DT_Int (creatures.iff difficulty/niche fields).
                assert kind[0] in "ibfheI", kind
                value = struct.unpack_from("<f" if kind[0] == "f" else
                                           "<I" if kind[0] == "I" else "<i", data, offset)[0]
                offset += 4
            row[column] = value
        rows.append(row)
    assert offset == len(data), "Unread table data"
    return columns, types, rows


def crc_catalog(path):
    outer = list(blocks(path.read_bytes()))
    assert len(outer) == 1 and outer[0][1][:4] == b"CSTB"
    version = list(blocks(outer[0][1][4:]))
    assert len(version) == 1 and version[0][1][:4] == b"0000"
    chunks = dict(blocks(version[0][1][4:]))
    count = struct.unpack("<I", chunks[b"DATA"])[0]
    keys = struct.unpack(f"<{count}I", chunks[b"CRCT"])
    offsets = struct.unpack(f"<{count}I", chunks[b"STRT"])
    assert list(keys) == sorted(set(keys)), "CRC catalog must be unique and sorted"
    return {key: chunks[b"STNG"][offset:].split(b"\0", 1)[0].decode()
            for key, offset in zip(keys, offsets)}


def block_positions(data, start=0, end=None):
    end = len(data) if end is None else end
    pos = start
    while pos < end:
        if pos + 8 > end:
            raise ValueError("truncated IFF header")
        tag = data[pos:pos + 4]
        size = struct.unpack_from(">I", data, pos + 4)[0]
        body = pos + 8
        stop = body + size
        if stop > end:
            raise ValueError("IFF block exceeds parent")
        yield tag, body, stop
        # SOE Iff writes exact block lengths, including odd-sized forms.
        pos = stop
    if pos != end:
        raise ValueError("misaligned IFF block")


def form(data, tag, body, stop):
    if tag != b"FORM" or data[body:body + 4] == b"":
        raise ValueError("expected IFF FORM")
    return data[body:body + 4], list(block_positions(data, body + 4, stop))
