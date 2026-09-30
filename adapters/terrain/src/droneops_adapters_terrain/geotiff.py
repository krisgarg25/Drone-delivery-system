"""Minimal GeoTIFF reader and writer: uncompressed, single-band, classic TIFF.

Why hand-rolled: the honest options for reading a Copernicus GLO-30 tile are GDAL,
rasterio or a PostGIS raster - none of which this workspace has, and a Phase 1 adapter
may not add a dependency by stealth. DEM tiles for one district are small, single-band
and usually delivered uncompressed or internally-compressed; the uncompressed case is
what this reads, and anything else raises rather than returning a plausible-looking
grid of the wrong numbers.

If a raster needs more than this (compression, multi-band, BigTIFF, overviews), the
correct move is ``gdal_translate -co COMPRESS=NONE`` into a path this adapter accepts -
documented in the adapter README - not a silent half-implementation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import TerrainError

TAGS = {
    256: "image_width",
    257: "image_length",
    258: "bits_per_sample",
    259: "compression",
    262: "photometric",
    273: "strip_offsets",
    277: "samples_per_pixel",
    278: "rows_per_strip",
    279: "strip_byte_counts",
    322: "tile_width",
    323: "tile_length",
    324: "tile_offsets",
    325: "tile_byte_counts",
    339: "sample_format",
    33550: "model_pixel_scale",
    33551: "geo_ascii_params",
    33922: "model_tiepoint",
    34264: "model_transformation",
    42113: "gdal_nodata",
}

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_SAMPLE_DTYPE = {
    1: {8: np.uint8, 16: np.int16, 32: np.int32, 64: np.int64},
    2: {8: np.uint8, 16: np.uint16, 32: np.uint32, 64: np.uint64},
    3: {32: np.float32, 64: np.float64},
    4: {16: np.float16, 32: np.complex64, 64: np.complex128},
}


@dataclass
class GeoTiff:
    """A single-band raster with its affine georeferencing."""

    width: int
    height: int
    data: np.ndarray
    pixel_size_x: float
    pixel_size_y: float
    origin_x: float
    origin_y: float
    nodata: float | None
    sample_format: int
    bits_per_sample: int
    geo_ascii_params: str
    path: str = ""
    tags: dict[str, Any] | None = None

    @property
    def resolution_degrees(self) -> float:
        return abs(self.pixel_size_x)

    def bbox(self) -> tuple[float, float, float, float]:
        left = self.origin_x
        top = self.origin_y
        return (left, top - abs(self.pixel_size_y) * self.height, left + self.pixel_size_x * self.width, top)

    def pixel_of(self, x: float, y: float) -> tuple[int, int]:
        col = int((x - self.origin_x) / self.pixel_size_x)
        row = int((y - self.origin_y) / self.pixel_size_y)
        return col, row


def read_geotiff(path: Path) -> GeoTiff:
    data = Path(path).read_bytes()
    if len(data) < 8:
        raise TerrainError(f"{path}: too small to be a TIFF")
    order = data[:2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        raise TerrainError(f"{path}: not a TIFF (magic {order!r})")
    magic = struct.unpack(endian + "H", data[2:4])[0]
    ifd_offset = struct.unpack(endian + "I", data[4:8])[0]
    if magic == 43:
        raise TerrainError(
            f"{path}: BigTIFF is not supported by this reader. Convert with "
            "gdal_translate -co COMPRESS=NONE to a classic GeoTIFF."
        )
    if magic != 42:
        raise TerrainError(f"{path}: bad TIFF magic {magic}")

    entries = _read_ifd(data, ifd_offset, endian)
    tags = _decode_entries(data, entries, endian)

    width = _one(tags["image_width"])
    height = _one(tags["image_length"])
    samples = _one(tags.get("samples_per_pixel", 1))
    if samples != 1:
        raise TerrainError(
            f"{path}: {samples} bands. This adapter models ground elevation, one band; "
            "a multi-band raster must be reduced by whoever owns its band semantics."
        )
    compression = _one(tags.get("compression", 1))
    if compression != 1:
        raise TerrainError(
            f"{path}: compression scheme {compression} is not implemented. Re-export "
            "uncompressed (gdal_translate -co COMPRESS=NONE) rather than guessing."
        )
    bits = _one(tags["bits_per_sample"]) if "bits_per_sample" in tags else 16
    sample_format = _one(tags.get("sample_format", 1))
    dtype = _SAMPLE_DTYPE.get(sample_format, {}).get(bits)
    if dtype is None:
        raise TerrainError(f"{path}: unsupported sample format {sample_format} at {bits} bits")

    array = _read_pixels(data, tags, endian, dtype, width, height, bits)

    scale = tags.get("model_pixel_scale")
    tie = tags.get("model_tiepoint")
    transform = tags.get("model_transformation")
    if scale is None or (tie is None and transform is None):
        raise TerrainError(
            f"{path}: no ModelPixelScale/ModelTiepoint tags. A raster without "
            "georeferencing has no relationship to the ground, so clipping it would "
            "produce a terrain surface attached to the wrong place."
        )
    pixel_x, pixel_y = float(scale[0]), float(scale[1])
    if tie is not None:
        origin_x, origin_y = float(tie[3]), float(tie[4])
        i_px, j_px = float(tie[0]), float(tie[1])
        origin_x -= i_px * pixel_x
        origin_y -= j_px * pixel_y
    else:
        origin_x = float(transform[12])
        origin_y = float(transform[13])

    nodata_raw = tags.get("gdal_nodata")
    nodata = float(str(nodata_raw).strip()) if nodata_raw not in (None, "") else None
    geo_ascii = str(tags.get("geo_ascii_params", "") or "")

    return GeoTiff(
        width=width,
        height=height,
        data=array,
        pixel_size_x=pixel_x,
        pixel_size_y=pixel_y,
        origin_x=origin_x,
        origin_y=origin_y,
        nodata=nodata,
        sample_format=sample_format,
        bits_per_sample=bits,
        geo_ascii_params=geo_ascii,
        path=str(path),
        tags={key: value for key, value in tags.items() if key in {"geo_ascii_params"}},
    )


def _read_ifd(data: bytes, offset: int, endian: str) -> list[tuple[int, int, int, bytes]]:
    if offset + 2 > len(data):
        raise TerrainError("IFD offset runs past the end of the file")
    count = struct.unpack(endian + "H", data[offset : offset + 2])[0]
    entries = []
    for index in range(count):
        start = offset + 2 + index * 12
        raw = data[start : start + 12]
        if len(raw) < 12:
            break
        tag, type_id = struct.unpack(endian + "HH", raw[:4])
        length = struct.unpack(endian + "I", raw[4:8])[0]
        payload = raw[8:12]
        entries.append((tag, type_id, length, payload))
    return entries


def _decode_entries(data: bytes, entries: list[Any], endian: str) -> dict[str, Any]:
    tags: dict[str, Any] = {}
    for tag, type_id, length, payload in entries:
        name = TAGS.get(tag)
        if name is None:
            continue
        size = _TYPE_SIZES[type_id] * length
        if size <= 4:
            raw = payload[:size]
        else:
            offset = struct.unpack(endian + "I", payload)[0]
            raw = data[offset : offset + size]
        tags[name] = _cast(raw, type_id, length, endian)
    return tags


def _cast(raw: bytes, type_id: int, length: int, endian: str) -> Any:
    fmt = {1: "B", 2: "s", 3: "H", 4: "I", 6: "b", 7: "B", 8: "h", 9: "i", 11: "f", 12: "d"}[type_id]
    if type_id == 2:
        return raw.split(b"\x00")[0].decode("ascii", "replace")
    values = struct.unpack(endian + f"{length}{fmt}", raw)
    return values[0] if length == 1 else list(values)


def _one(value: Any) -> int:
    if isinstance(value, (list, tuple)):
        return int(value[0])
    return int(value)


def _read_pixels(data: bytes, tags: dict[str, Any], endian: str, dtype: Any, width: int, height: int, bits: int) -> np.ndarray:
    if "strip_offsets" in tags:
        offsets = tags["strip_offsets"]
        offsets = offsets if isinstance(offsets, list) else [offsets]
        counts = tags.get("strip_byte_counts", [])
        counts = counts if isinstance(counts, list) else [counts]
        rows_per_strip = _one(tags.get("rows_per_strip", height))
        chunks = []
        for index, offset in enumerate(offsets):
            size = int(counts[index]) if index < len(counts) else (width * bits // 8) * min(
                rows_per_strip, height - index * rows_per_strip
            )
            chunks.append(data[offset : offset + size])
        return np.frombuffer(b"".join(chunks), dtype=dtype).reshape(height, width).copy()
    if "tile_offsets" in tags:
        tile_w = _one(tags["tile_width"])
        tile_h = _one(tags["tile_length"])
        offsets = tags["tile_offsets"]
        offsets = offsets if isinstance(offsets, list) else [offsets]
        array = np.zeros((height, width), dtype=dtype)
        per_row = max(1, width // tile_w)
        for index, offset in enumerate(offsets):
            ty, tx = divmod(index, per_row)
            raw = np.frombuffer(data[offset : offset + tile_w * tile_h * array.itemsize], dtype=dtype)
            block = raw.reshape(tile_h, tile_w)
            rows = min(tile_h, height - ty * tile_h)
            cols = min(tile_w, width - tx * tile_w)
            array[ty * tile_h : ty * tile_h + rows, tx * tile_w : tx * tile_w + cols] = block[:rows, :cols]
        return array
    raise TerrainError("raster has neither strip nor tile offsets")
# ------------------------------------------------------------------------ writing

def write_geotiff(
    path: Path,
    array: np.ndarray,
    *,
    origin_x: float,
    origin_y: float,
    pixel_size_x: float,
    pixel_size_y: float,
    nodata: float | None = None,
    geo_ascii: str = 'GEOGCS["WGS 84"]',
) -> int:
    """Write an uncompressed single-band Float32 GeoTIFF. Returns the byte count.

    The reader above and this writer are round-trip tested against each other, which
    proves this adapter's own clipping is coherent. It does not prove a third-party
    consumer reads the file identically; anything shipping this raster elsewhere
    re-validates it with GDAL there.
    """
    if array.ndim != 2:
        raise TerrainError("only single-band arrays can be written")
    height, width = array.shape
    endian = "<"
    payload = np.ascontiguousarray(array, dtype=np.float32).tobytes()
    data_offset = 8
    extras_offset = data_offset + len(payload)
    extras = bytearray()
    entries: list[tuple[int, int, int, bytes, int | None]] = []

    def inline(tag: int, type_id: int, count: int, packed: bytes) -> None:
        entries.append((tag, type_id, count, packed + b"\x00" * (4 - len(packed)), None))

    def external(tag: int, type_id: int, raw: bytes, count: int) -> None:
        offset = extras_offset + len(extras)
        extras.extend(raw)
        entries.append((tag, type_id, count, None, offset))

    inline(256, 3, 1, struct.pack(endian + "H", width))
    inline(257, 3, 1, struct.pack(endian + "H", height))
    inline(258, 3, 1, struct.pack(endian + "H", 32))
    inline(259, 3, 1, struct.pack(endian + "H", 1))  # no compression
    inline(262, 3, 1, struct.pack(endian + "H", 1))  # BlackIsZero
    inline(273, 4, 1, struct.pack(endian + "I", data_offset))
    inline(277, 3, 1, struct.pack(endian + "H", 1))  # one sample per pixel
    inline(278, 3, 1, struct.pack(endian + "H", height))  # one strip
    inline(279, 4, 1, struct.pack(endian + "I", len(payload)))
    inline(339, 3, 1, struct.pack(endian + "H", 3))  # IEEE float
    external(33550, 12, struct.pack(endian + "3d", abs(pixel_size_x), abs(pixel_size_y), 0.0), 3)
    external(33922, 12, struct.pack(endian + "6d", 0.0, 0.0, 0.0, origin_x, origin_y, 0.0), 6)
    ascii_params = (geo_ascii + "\x00").encode("ascii", "replace")
    if nodata is not None:
        external(42113, 2, f"{nodata}\x00".encode("ascii"), len(f"{nodata}\x00"))
    external(33551, 2, ascii_params, len(ascii_params))

    ifd_offset = extras_offset + len(extras)
    ifd = bytearray(struct.pack(endian + "H", len(entries)))
    for tag, type_id, count, packed, offset in sorted(entries, key=lambda item: item[0]):
        ifd += struct.pack(endian + "HHI", tag, type_id, count)
        ifd += struct.pack(endian + "I", offset) if offset is not None else packed
    ifd += struct.pack(endian + "I", 0)

    out = bytearray(struct.pack(endian + "HHI", 0x4949, 42, ifd_offset))
    out += payload
    out += extras
    out += ifd
    Path(path).write_bytes(bytes(out))
    return len(out)
