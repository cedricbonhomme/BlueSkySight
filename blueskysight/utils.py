import base64
import hashlib
import httpx
import io
import struct
from enum import Enum


def remove_case_insensitive_duplicates(input_list):
    """Remove duplicates in a list, ignoring case.
    This approach preserves the last occurrence of each unique item based on
    lowercase equivalence. The dictionary keys are all lowercase to ensure
    case-insensitive comparison, while the original case is preserved in the output.
    """
    return list({item.lower(): item for item in input_list}.values())


async def resolve_did_to_handle(did):
    """Resolve a DID to a handle using the Bluesky API."""
    url = f"https://bsky.social/xrpc/app.bsky.identity.resolveHandle?did={did}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        if response.status_code == 200:
            data = response.json()
            return data.get("handle")
        else:
            print(f"Failed to resolve DID {did}: {response.status_code}")
            return None


async def get_url_from_uri(uri):
    """Convert an AT Protocol URI to a public Bluesky URL."""
    parts = uri.split("/")
    did = parts[2]
    post_id = parts[-1]

    handle = await resolve_did_to_handle(did)
    if handle:
        return f"https://bsky.app/profile/{handle}/post/{post_id}"
    else:
        print("Failed to resolve handle.")
        return None


# ground control to major type
class MajorType(Enum):
    UNSIGNED_INT = 0
    NEGATIVE_INT = 1
    BYTE_STRING = 2
    TEXT_STRING = 3
    ARRAY = 4
    MAP = 5
    TAG = 6
    FLOAT = 7


async def parse_cbor_head(stream):
    first = stream.read(1)[0]
    major_type = MajorType(first >> 5)
    additional_info = first & 0x1F

    if additional_info < 24:
        if major_type == major_type.FLOAT:
            return major_type, {20: False, 21: True, 22: None}[additional_info]  # null
        return major_type, additional_info

    BYTE_LENGTHS = {24: 1, 25: 2, 26: 4, 27: 8}
    if additional_info in BYTE_LENGTHS:
        byte_value = stream.read(BYTE_LENGTHS[additional_info])
        if major_type == MajorType.NEGATIVE_INT:
            return major_type, -1 - int.from_bytes(
                byte_value, "big"
            )  # TODO: check canonical-ness
        if major_type == MajorType.FLOAT:
            if len(byte_value) == 1:
                raise Exception("invalid")
            if len(byte_value) == 2:
                return major_type, struct.unpack("!e", byte_value)[0]
            if len(byte_value) == 4:
                return major_type, struct.unpack("!f", byte_value)[0]
            if len(byte_value) == 8:
                return major_type, struct.unpack("!d", byte_value)[0]
            raise Exception("unreachable")

        return major_type, int.from_bytes(
            byte_value, "big"
        )  # TODO: check canonical-ness

    if additional_info == 31:
        raise Exception("indefinite lengths not supported by this implementation")

    raise Exception("not well-formed")


# LEB128
def parse_varint(stream):
    n = 0
    shift = 0
    while True:
        val = stream.read(1)[0]
        n |= (val & 0x7F) << shift
        if not val & 0x80:
            return n
        shift += 7


# parse into pythonic objects (not roundtrip-safe, for now)
async def parse_dag_cbor_object(stream):
    major_type, info = await parse_cbor_head(stream)
    if major_type in [MajorType.UNSIGNED_INT, MajorType.NEGATIVE_INT, MajorType.FLOAT]:
        return info

    if major_type == MajorType.BYTE_STRING:
        value = stream.read(info)
        if len(value) != info:
            raise EOFError()
        return value

    if major_type == MajorType.TEXT_STRING:
        value = stream.read(info)
        if len(value) != info:
            raise EOFError()
        return value.decode()

    if major_type == MajorType.ARRAY:
        values = []
        for _ in range(info):
            values.append(await parse_dag_cbor_object(stream))
        return values

    if major_type == MajorType.MAP:
        values = {}
        for _ in range(info):
            key = await parse_dag_cbor_object(stream)
            if type(key) != str:
                raise ValueError("DAG-CBOR only accepts strings as map keys")
            values[key] = await parse_dag_cbor_object(stream)
        # TODO: check canonical map ordering
        return values

    if major_type == MajorType.TAG:
        if info != 42:
            raise Exception("non-42 tags are not supported")
        cid_bytes = await parse_dag_cbor_object(stream)
        assert type(cid_bytes) is bytes
        assert len(cid_bytes) == 37
        assert cid_bytes.startswith(b"\x00\x01q\x12 ") or cid_bytes.startswith(
            b"\x00\x01U\x12 "
        )  # multibase prefix, CIDv1, dag-cbor or raw,  sha256
        return "b" + base64.b32encode(cid_bytes[1:]).decode().lower().rstrip("=")


async def parse_car(stream, length):
    header_len = parse_varint(stream)
    header_start = stream.tell()
    car_header = await parse_dag_cbor_object(stream)
    assert stream.tell() - header_start == header_len
    assert car_header.get("version") == 1
    assert len(car_header.get("roots", [])) == 1
    root = car_header["roots"][0]

    nodes = {}

    while stream.tell() != length:
        block_len = parse_varint(stream)
        block_start = stream.tell()  # XXX: what if length is less than 36?
        cid_raw = stream.read(
            36
        )  # XXX: this needs to be parsed properly, length might not be 36
        assert cid_raw.startswith(b"\x01q\x12 ")  # CIDv1, dag-cbor, sha256
        cid = "b" + base64.b32encode(cid_raw).decode().lower().rstrip("=")
        block_data = stream.read(block_len - 36)
        content_hash = hashlib.sha256(block_data).digest()
        assert cid_raw.endswith(content_hash)
        block_data_stream = io.BytesIO(block_data)
        block = await parse_dag_cbor_object(block_data_stream)
        assert not block_data_stream.read()
        assert stream.tell() - block_start == block_len
        nodes[cid] = block

    return root, nodes


async def enumerate_mst_records(nodes, node):
    records = {}
    if node.get("l") in nodes:
        records |= await enumerate_mst_records(nodes, nodes[node["l"]])
    prev_key = b""
    for entry in node["e"]:
        assert entry["p"] <= len(prev_key)
        key = prev_key[: entry["p"]] + entry["k"]
        prev_key = key
        records[key] = entry["v"]
        if entry.get("t") in nodes:
            records |= await enumerate_mst_records(nodes, nodes[entry["t"]])
    return records