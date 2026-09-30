"""Local-format readers: OSM PBF *header*, ESRI shapefile + DBF, GeoJSON/JSONL.

Scope decision, stated because it is load-bearing:

* **PBF** - this module decodes the ``OSMHeader`` block, which carries the replication
  timestamp (the extract date), the extract bbox, the writing program and the source
  string. Those are exactly the fields the manifest needs, and they are verifiable:
  the header carries the bbox twice, in nanodegrees and in 1e-14 degrees, and the two
  must agree.
  It does **not** decode ``OSMData`` blocks. Doing that means the whole of
  ``osmformat.proto`` including dense-node delta-delta tag streams, and a partly
  correct tag decoder would silently mis-assign heights to buildings - the one failure
  this dataset cannot afford. Clip with ``osmium extract`` and hand the adapter a
  GeoJSON (documented in the adapter README), or use a shapefile extract.
* **SHP/DBF** - fully implemented; this is what Geofabrik publishes as a clipped
  per-layer extract and what a survey office is most likely to hand over.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import GisEtlError

_PBF_NANODEG = 1e9
_PBF_FEMTODEG = 1e14
_WIRE_VARINT, _WIRE_FIXED64, _WIRE_LEN, _WIRE_FIXED32 = 0, 1, 2, 5


@dataclass
class PbfHeader:
    """The verified contents of a PBF ``OSMHeader`` block."""

    path: str
    writing_program: str = ""
    source: str = ""
    replication_timestamp: int | None = None
    replication_base_url: str = ""
    required_features: list[str] = field(default_factory=list)
    optional_features: list[str] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None
    bbox_verified: bool = False
    extract_date: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "writing_program": self.writing_program,
            "source": self.source,
            "replication_timestamp": self.replication_timestamp,
            "replication_timestamp_iso": self.extract_date,
            "replication_base_url": self.replication_base_url,
            "required_features": sorted(self.required_features),
            "bbox_degrees": list(self.bbox) if self.bbox else None,
            "bbox_cross_checked": self.bbox_verified,
        }


def read_pbf_header(path: Path) -> PbfHeader:
    """Read the first FileBlock and decode its OSMHeader blob."""
    with path.open("rb") as handle:
        preamble = handle.read(4)
        if len(preamble) < 4:
            raise GisEtlError(f"{path}: too short to be a PBF file")
        header_size = struct.unpack(">i", preamble)[0]
        if not 0 < header_size <= 64 * 1024:
            raise GisEtlError(f"{path}: implausible BlobHeader size {header_size}")
        header_bytes = handle.read(header_size)
        blob_header = _parse_message(header_bytes)
        block_type = _as_text(_single(blob_header[3]))
        data_size = _as_int(_single(blob_header[4]))
        if block_type != "OSMHeader":
            raise GisEtlError(
                f"{path}: first block is {block_type!r}, not OSMHeader; not a PBF file "
                "or the block order is unexpected. Refusing to guess an extract date."
            )
        blob_bytes = handle.read(data_size)
    blob = _parse_message(blob_bytes)
    raw = blob.get(1, [None])[0]
    zlib_data = blob.get(3, [None])[0]
    if zlib_data:
        payload = zlib.decompress(zlib_data)
        declared_raw_size = _as_int(_single(blob[2])) if 2 in blob else 0
        if declared_raw_size and declared_raw_size != len(payload):
            raise GisEtlError(f"{path}: blob raw_size {declared_raw_size} != decompressed {len(payload)}")
    elif raw:
        payload = raw
    else:
        raise GisEtlError(
            f"{path}: OSMHeader blob carries neither raw nor zlib_data. It may use a "
            "compression this reader does not implement (lzma/zstd); the extract date "
            "cannot be read from it."
        )
    return _decode_header_block(payload, path)


def _decode_header_block(payload: bytes, path: Path) -> PbfHeader:
    fields = _parse_message(payload)
    header = PbfHeader(path=str(path))
    if 1 in fields:
        header.bbox, header.bbox_verified = _decode_bbox(_single(fields[1]))
    header.required_features = [_as_text(item) for item in fields.get(4, [])]
    header.optional_features = [_as_text(item) for item in fields.get(5, [])]
    if 16 in fields:
        header.writing_program = _as_text(_single(fields[16]))
    if 17 in fields:
        header.source = _as_text(_single(fields[17]))
    if 20 in fields:
        # osmosis_replication_timestamp is a plain int64 varint, not zig-zag.
        header.replication_timestamp = _as_int(_single(fields[20]))
    if 22 in fields:
        header.replication_base_url = _as_text(_single(fields[22]))
    if header.replication_timestamp:
        header.extract_date = datetime.fromtimestamp(
            header.replication_timestamp, tz=timezone.utc
        ).isoformat()
    return header


def _decode_bbox(value: Any) -> tuple[tuple[float, float, float, float], bool]:
    bbox = _parse_message(value)
    # HeaderBBox fields 1-4 are sint64 nanodegrees: zig-zag encoded.
    left = _as_sint(_single(bbox[1])) / _PBF_NANODEG
    right = _as_sint(_single(bbox[2])) / _PBF_NANODEG
    top = _as_sint(_single(bbox[3])) / _PBF_NANODEG
    bottom = _as_sint(_single(bbox[4])) / _PBF_NANODEG
    if not all(-180.0 <= x <= 180.0 for x in (left, right)) or not all(
        -90.0 <= x <= 90.0 for x in (top, bottom)
    ):
        raise GisEtlError(
            f"PBF header bbox {left},{bottom},{right},{top} is not in degrees or nanodegrees; "
            "the unit assumption of this reader does not hold for this file."
        )
    verified = True
    for fine, coarse in ((5, left), (6, right), (7, top), (8, bottom)):
        if fine in bbox:
            femto = _as_fixed64(_single(bbox[fine])) / _PBF_FEMTODEG
            if abs(femto - coarse) > 1e-6:
                verified = False
    return (left, bottom, right, top), verified


# --------------------------------------------------------------------- shapefile


@dataclass
class ShapeRecord:
    """One shapefile geometry, paired with its DBF attribute row if one exists."""

    shape_type: int
    geometry: dict[str, Any]
    attributes: dict[str, str]


class ShapefileReader:
    """ESRI shapefile + dBase attributes, no third-party dependency.

    Supported geometry types: Point (1), PolyLine (3), Polygon (5), MultiPoint (8) and
    their Z/M variants, whose extra dimension ranges are read and discarded - an
    elevation M value in a shapefile is *not* an obstacle height we can trust, and
    treating it as one would be exactly the kind of quiet promotion this project bans.
    """

    _TYPES = {0: "null", 1: "Point", 3: "PolyLine", 5: "Polygon", 8: "MultiPoint"}

    def __init__(self, shp_path: Path, *, dbf_path: Path | None = None, encoding: str = "utf-8"):
        self.shp_path = Path(shp_path)
        self.dbf_path = dbf_path or self.shp_path.with_suffix(".dbf")
        self.encoding = encoding

    def read(self) -> list[ShapeRecord]:
        shapes = list(self._read_shapes())
        attributes = self._read_dbf() if self.dbf_path.is_file() else []
        if attributes and len(attributes) != len(shapes):
            raise GisEtlError(
                f"{self.shp_path.name}: {len(shapes)} shapes but {len(attributes)} DBF rows. "
                "A shapefile whose geometry and attribute counts disagree cannot be "
                "joined safely, so no attribute is attached to any geometry here."
            )
        return [
            ShapeRecord(shape_type=shape[0], geometry=shape[1], attributes=attributes[index] if attributes else {})
            for index, shape in enumerate(shapes)
        ]

    def _read_shapes(self) -> Iterator[tuple[int, dict[str, Any]]]:
        data = self.shp_path.read_bytes()
        if len(data) < 100:
            raise GisEtlError(f"{self.shp_path}: shorter than a shapefile header")
        magic, length = struct.unpack(">ii", data[:8])
        if magic != 9994:
            raise GisEtlError(f"{self.shp_path}: bad shapefile magic {magic}")
        declared = length * 2
        if declared != len(data):
            raise GisEtlError(
                f"{self.shp_path}: header declares {declared} bytes, file has {len(data)}; "
                "the extract is truncated and its features must not be published"
            )
        offset = 100
        while offset < len(data):
            _record_number, content_length = struct.unpack(">ii", data[offset : offset + 8])
            offset += 8
            end = offset + content_length * 2
            if end > len(data):
                raise GisEtlError(f"{self.shp_path}: record runs past end of file at {offset}")
            shape_type = struct.unpack("<i", data[offset : offset + 4])[0]
            body = data[offset + 4 : end]
            offset = end
            geometry = self._decode(shape_type, body)
            if geometry is not None:
                yield shape_type, geometry

    def _decode(self, shape_type: int, body: bytes) -> dict[str, Any] | None:
        base = shape_type % 1000
        if base == 0:
            return None
        if base in (11, 13, 15, 18, 31, 33, 35, 38):
            # Z/M variants: skip the 8 doubles of range/measures before the payload.
            body = body[64:] if base in (11, 13, 15, 18) else body[32:]
        if base in (1, 11, 21, 31):
            x, y = struct.unpack("<dd", body[:16])
            return {"type": "Point", "coordinates": [x, y]}
        if base in (8, 18, 28):
            count = struct.unpack("<i", body[:4])[0]
            coords = struct.unpack(f"<{count * 2}d", body[4 : 4 + count * 16])
            return {
                "type": "MultiPoint",
                "coordinates": [[coords[i], coords[i + 1]] for i in range(0, len(coords), 2)],
            }
        if base in (3, 5, 13, 15, 23, 25, 33, 35):
            parts_count, points_count = struct.unpack("<ii", body[:8])
            parts = list(struct.unpack(f"<{parts_count}i", body[8 : 8 + 4 * parts_count]))
            start = 8 + 4 * parts_count
            coords = struct.unpack(f"<{points_count * 2}d", body[start : start + points_count * 16])
            rings: list[list[list[float]]] = []
            for index, part_start in enumerate(parts):
                part_end = parts[index + 1] if index + 1 < len(parts) else points_count
                rings.append(
                    [[coords[i], coords[i + 1]] for i in range(part_start * 2, part_end * 2, 2)]
                )
            if base in (3, 13, 23, 33):
                return (
                    {"type": "LineString", "coordinates": rings[0]}
                    if len(rings) == 1
                    else {"type": "MultiLineString", "coordinates": rings}
                )
            polygons = _assemble_polygons(rings)
            if len(polygons) == 1:
                return {"type": "Polygon", "coordinates": polygons[0]}
            return {"type": "MultiPolygon", "coordinates": polygons}
        raise GisEtlError(f"{self.shp_path}: unsupported shape type {shape_type}")

    def _read_dbf(self) -> list[dict[str, str]]:
        data = self.dbf_path.read_bytes()
        if len(data) < 32 or data[0] == 0x1A:
            return []
        record_count = struct.unpack("<I", data[4:8])[0]
        header_length = struct.unpack("<H", data[8:10])[0]
        record_length = struct.unpack("<H", data[10:12])[0]
        fields: list[tuple[str, int]] = []
        offset = 32
        while offset < header_length - 1 and data[offset] != 0x0D:
            name = data[offset : offset + 11].split(b"\x00")[0].decode("utf-8", "replace")
            size = data[offset + 16]
            fields.append((name, size))
            offset += 32
        rows: list[dict[str, str]] = []
        cursor = header_length
        for _ in range(record_count):
            if cursor + record_length > len(data):
                break
            if data[cursor] != 0x2A:  # a deleted record ('*') is skipped, not zeroed
                row: dict[str, str] = {}
                position = cursor + 1
                for name, size in fields:
                    raw = data[position : position + size]
                    position += size
                    text = raw.decode(self.encoding, "replace").strip()
                    if text:
                        row[name] = text
                rows.append(row)
            cursor += record_length
        return rows


def _assemble_polygons(rings: list[list[list[float]]]) -> list[list[list[list[float]]]]:
    """Shapefile rings are outer/inner by winding order; rebuild GeoJSON polygons."""
    outers: list[list[list[list[float]]]] = []
    holes: list[list[list[float]]] = []
    for ring in rings:
        if _signed_area(ring) > 0:
            outers.append([ring])
        else:
            holes.append(ring)
    if not outers:
        return []
    if len(outers) == 1:
        outers[0].extend(holes)
        return outers
    for hole in holes:
        centroid = [sum(p[0] for p in hole) / len(hole), sum(p[1] for p in hole) / len(hole)]
        for polygon in outers:
            if _point_in_ring(centroid, polygon[0]):
                polygon.append(hole)
                break
    return outers


def _signed_area(ring: list[list[float]]) -> float:
    total = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index][0], ring[index][1]
        x2, y2 = ring[index + 1][0], ring[index + 1][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _point_in_ring(point: list[float], ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > point[1]) != (yj > point[1])) and (
            point[0] < (xj - xi) * (point[1] - yi) / (yj - yi + 1e-18) + xi
        ):
            inside = not inside
        j = i
    return inside


# ------------------------------------------------------------------ geojson/jsonl


def read_geojson_features(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") == "FeatureCollection":
        return list(payload.get("features", []))
    if payload.get("type") == "Feature":
        return [payload]
    if payload.get("type") in {"Polygon", "MultiPolygon", "LineString", "Point"}:
        return [{"type": "Feature", "geometry": payload, "properties": {}}]
    raise GisEtlError(f"{path}: not a GeoJSON feature collection")


def read_jsonl_features(path: Path) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            features.append(record if record.get("type") == "Feature" else {"type": "Feature", "geometry": record, "properties": {}})
    return features


# ---------------------------------------------------------------- protobuf reading



def _parse_message(data: bytes) -> dict[int, list[Any]]:
    """Minimal protobuf reader. Repeated fields stay a list; nothing is guessed."""
    values: dict[int, list[Any]] = {}
    position = 0
    while position < len(data):
        tag, position = _read_varint(data, position)
        number, wire = tag >> 3, tag & 0x07
        if wire == _WIRE_VARINT:
            value, position = _read_varint(data, position)
        elif wire == _WIRE_LEN:
            size, position = _read_varint(data, position)
            value = data[position : position + size]
            position += size
        elif wire == _WIRE_FIXED64:
            value = data[position : position + 8]
            position += 8
        elif wire == _WIRE_FIXED32:
            value = data[position : position + 4]
            position += 4
        elif wire == 4:  # end group: not used by OSM PBF
            raise GisEtlError("PBF header uses groups, which this reader does not support")
        else:
            raise GisEtlError(f"unknown protobuf wire type {wire} in field {number}")
        values.setdefault(number, []).append(value)
    return values


def _single(items: list[Any]) -> Any:
    """An optional field's value. Two occurrences means the file is malformed."""
    if len(items) > 1:
        raise GisEtlError(
            f"expected one value, found {len(items)}; this PBF header does not match the "
            "field layout this reader assumes, so its extract date is not trustworthy"
        )
    return items[0]


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if position >= len(data):
            raise GisEtlError("truncated protobuf varint")
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return _signed_from_varint(result, shift), position
        shift += 7
        if shift > 70:
            raise GisEtlError("protobuf varint exceeds 64 bits")


def _signed_from_varint(number: int, shift: int) -> int:
    """A 10-byte varint carries an int64; above 2**63-1 it is a negative value."""
    if number >= 1 << 63:
        return number - (1 << 64)
    return number


def _as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, bytes) and len(value) == 8:
        return struct.unpack("<q", value)[0]
    return 0


def _as_sint(value: Any) -> int:
    """Zig-zag decode of a protobuf sint64. HeaderBBox uses sint64; a timestamp does not."""
    number = _as_int(value)
    return (number >> 1) ^ -(number & 1)


def _as_fixed64(value: Any) -> float:
    if isinstance(value, bytes) and len(value) == 8:
        return struct.unpack("<d", value)[0]
    return 0.0


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, list):
        return ",".join(_as_text(item) for item in value)
    return str(value)
