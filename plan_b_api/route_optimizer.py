"여행 일정 동선 최적화 모듈 (TSP / Route Optimization)."

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Sequence


def haversine_distance_meters(
    lon1: float, lat1: float, lon2: float, lat2: float
) -> float:
    "두 좌표 사이의 구면 거리(미터)를 계산한다."
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def total_route_distance(
    points: Sequence[tuple[float, float]],
) -> float:
    "주어진 좌표 순서의 총 이동거리(미터)를 계산한다."
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(len(points) - 1):
        total += haversine_distance_meters(
            points[i][0], points[i][1], points[i + 1][0], points[i + 1][1]
        )
    return total


def _extract_coord(item: Any) -> tuple[float, float]:
    if isinstance(item, dict):
        if "longitude" in item and "latitude" in item:
            try:
                return float(item["longitude"]), float(item["latitude"])
            except (ValueError, TypeError):
                pass
        place = item.get("place")
        if isinstance(place, dict) and "longitude" in place and "latitude" in place:
            try:
                return float(place["longitude"]), float(place["latitude"])
            except (ValueError, TypeError):
                pass
    return (0.0, 0.0)


@dataclass(frozen=True)
class OptimizationResult:
    optimized_indices: list[int]
    original_distance_meters: float
    optimized_distance_meters: float
    saved_distance_meters: float
    estimated_saved_minutes: float
    is_improved: bool
    items: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "optimized_indices": self.optimized_indices,
            "original_distance_meters": round(self.original_distance_meters, 1),
            "optimized_distance_meters": round(self.optimized_distance_meters, 1),
            "saved_distance_meters": round(self.saved_distance_meters, 1),
            "estimated_saved_minutes": round(self.estimated_saved_minutes, 1),
            "is_improved": self.is_improved,
            "items": self.items,
        }


def optimize_schedule_order(
    items: Sequence[dict[str, Any]],
    *,
    keep_first: bool = True,
    average_speed_kmh: float = 25.0,
) -> OptimizationResult:
    """일정 목록의 이동거리를 최소화하는 방문 순서를 계산한다.

    - locked=True 또는 fixed_arrival_time 이 있는 항목은 원래의 순서(인덱스)에 고정된다.
    - keep_first=True 인 경우, 0번째 항목(출발지)은 고정된다.
    - 미고정 항목 수가 8개 이하인 경우: 완전 탐색(Brute-force)으로 전역 최적해 보장.
    - 미고정 항목 수가 8개 초과인 경우: 2-Opt 휴리스틱 알고리즘 사용.
    """
    items_list = [dict(it) if isinstance(it, dict) else it for it in items]
    n = len(items_list)
    original_indices = list(range(n))

    if n < 3:
        coords = [_extract_coord(it) for it in items_list]
        dist = total_route_distance(coords)
        return OptimizationResult(
            optimized_indices=original_indices,
            original_distance_meters=dist,
            optimized_distance_meters=dist,
            saved_distance_meters=0.0,
            estimated_saved_minutes=0.0,
            is_improved=False,
            items=items_list,
        )

    coords = [_extract_coord(it) for it in items_list]
    orig_dist = total_route_distance(coords)

    fixed_indices: set[int] = set()
    if keep_first:
        fixed_indices.add(0)

    for i, item in enumerate(items_list):
        if bool(item.get("locked")) or bool(item.get("fixed_arrival_time")):
            fixed_indices.add(i)

    free_positions = [i for i in range(n) if i not in fixed_indices]
    free_items = [i for i in range(n) if i not in fixed_indices]

    if len(free_positions) <= 1:
        return OptimizationResult(
            optimized_indices=original_indices,
            original_distance_meters=orig_dist,
            optimized_distance_meters=orig_dist,
            saved_distance_meters=0.0,
            estimated_saved_minutes=0.0,
            is_improved=False,
            items=items_list,
        )

    best_order: list[int] = list(original_indices)
    best_distance = orig_dist

    if len(free_items) <= 8:
        for perm in itertools.permutations(free_items):
            candidate = list(original_indices)
            for pos, val in zip(free_positions, perm):
                candidate[pos] = val
            cand_coords = [coords[idx] for idx in candidate]
            cand_dist = total_route_distance(cand_coords)
            if cand_dist < best_distance:
                best_distance = cand_dist
                best_order = candidate
    else:
        current = list(original_indices)
        improved = True
        while improved:
            improved = False
            for i in range(len(free_positions) - 1):
                for j in range(i + 1, len(free_positions)):
                    cand = list(current)
                    sub_pos = free_positions[i : j + 1]
                    sub_vals = [cand[p] for p in sub_pos]
                    for p, v in zip(sub_pos, reversed(sub_vals)):
                        cand[p] = v
                    cand_coords = [coords[idx] for idx in cand]
                    cand_dist = total_route_distance(cand_coords)
                    if cand_dist < best_distance - 1.0:
                        best_distance = cand_dist
                        best_order = cand
                        current = cand
                        improved = True
                        break
                if improved:
                    break

    saved_dist = max(0.0, orig_dist - best_distance)
    saved_hours = (saved_dist / 1000.0) / average_speed_kmh
    saved_minutes = saved_hours * 60.0
    is_improved = saved_dist >= 10.0 and best_order != original_indices

    return OptimizationResult(
        optimized_indices=best_order,
        original_distance_meters=orig_dist,
        optimized_distance_meters=best_distance,
        saved_distance_meters=saved_dist,
        estimated_saved_minutes=saved_minutes,
        is_improved=is_improved,
        items=[items_list[i] for i in best_order],
    )
