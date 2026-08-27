"""서울시 공식 SHP/DBF 장소 영역을 런타임용 JSON으로 변환한다."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path


def read_dbf(path: Path) -> list[dict[str, str]]:
    data = path.read_bytes()
    record_count = struct.unpack_from("<I", data, 4)[0]
    header_length, record_length = struct.unpack_from("<HH", data, 8)
    fields: list[tuple[str, int]] = []
    position = 32
    while position < header_length - 1:
        descriptor = data[position : position + 32]
        name = descriptor[:11].split(b"\0", 1)[0].decode("ascii")
        fields.append((name, descriptor[16]))
        position += 32

    records: list[dict[str, str]] = []
    for index in range(record_count):
        start = header_length + index * record_length
        raw = data[start : start + record_length]
        if not raw or raw[0:1] == b"*":
            continue
        offset = 1
        record: dict[str, str] = {}
        for name, length in fields:
            value = raw[offset : offset + length]
            record[name] = value.decode("utf-8", errors="replace").strip()
            offset += length
        records.append(record)
    return records


def read_shp(path: Path) -> list[list[list[list[float]]]]:
    data = path.read_bytes()
    if struct.unpack_from("<I", data, 32)[0] != 5:
        raise ValueError("Polygon SHP만 지원합니다.")
    position = 100
    shapes: list[list[list[list[float]]]] = []
    while position + 8 <= len(data):
        _, words = struct.unpack_from(">II", data, position)
        position += 8
        content = data[position : position + words * 2]
        position += words * 2
        shape_type = struct.unpack_from("<I", content, 0)[0]
        if shape_type == 0:
            shapes.append([])
            continue
        if shape_type != 5:
            raise ValueError(f"지원하지 않는 SHP 타입: {shape_type}")
        part_count, point_count = struct.unpack_from("<II", content, 36)
        part_starts = list(
            struct.unpack_from(f"<{part_count}I", content, 44)
        )
        point_offset = 44 + part_count * 4
        points = [
            list(struct.unpack_from("<dd", content, point_offset + i * 16))
            for i in range(point_count)
        ]
        rings: list[list[list[float]]] = []
        for index, start in enumerate(part_starts):
            end = part_starts[index + 1] if index + 1 < part_count else point_count
            rings.append(points[start:end])
        shapes.append(rings)
    return shapes


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("사용법: convert_seoul_crowd_areas.py SHP_디렉터리 출력.json")
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    shp = next(source.glob("*.shp"))
    dbf = next(source.glob("*.dbf"))
    records = read_dbf(dbf)
    shapes = read_shp(shp)
    if len(records) != len(shapes):
        raise ValueError(f"속성 {len(records)}개와 영역 {len(shapes)}개가 다릅니다.")
    areas = [
        {
            "area_code": record["AREA_CD"],
            "category": record["CATEGORY"],
            "name": record["AREA_NM"],
            "rings": rings,
        }
        for record, rings in zip(records, shapes)
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(areas, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"{len(areas)}개 영역 저장: {output}")


if __name__ == "__main__":
    main()
