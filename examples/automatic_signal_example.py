"""정규화 장소에서 추천 점수 입력을 자동 생성하는 예제."""

from __future__ import annotations

from datetime import datetime

from plan_b_api import (
    CandidateFacts,
    NormalizedPlace,
    TripContext,
    evaluate_place_candidate,
    normalize_place,
    prompt_user_priorities,
)


def place(title: str, fee: int) -> NormalizedPlace:
    return normalize_place(
        {
            "common": [{
                "title": title,
                "mapx": "126.97",
                "mapy": "37.57",
                "firstimage": "https://example.com/image.jpg",
            }],
            "intro": [{
                "restdate": "매주 화요일",
                "usetime": "[1월~12월]09:00~18:00",
                "parking": "가능",
            }],
            "info": [
                {"infoname": "입장료", "infotext": f"대인 {fee:,}원"},
                {"infoname": "화장실", "infotext": "있음"},
            ],
        }
    )


trip = TripContext(
    arrival_time=datetime(2026, 7, 29, 14, 0),
    party_size=3,
    children_count=1,
    remaining_budget_krw=20_000,
    weather_severity=0.9,
    minutes_until_locked_stop=180,
)
candidates = {
    "야외 궁궐": (
        place("야외 궁궐", 3_000),
        CandidateFacts(
            indoor_ratio=0.1,
            route_minutes=15,
            walking_meters=1200,
            crowd_level=0.4,
            child_suitability=0.7,
            visit_minutes=70,
        ),
    ),
    "실내 박물관": (
        place("실내 박물관", 5_000),
        CandidateFacts(
            indoor_ratio=0.95,
            route_minutes=12,
            walking_meters=500,
            crowd_level=0.3,
            child_suitability=0.95,
            visit_minutes=70,
        ),
    ),
}

def main() -> None:
    priorities = prompt_user_priorities()
    print("\n선택 결과:", priorities.as_dict())

    for name, (normalized_place, candidate_facts) in candidates.items():
        result = evaluate_place_candidate(
            normalized_place, candidate_facts, trip, priorities
        )
        print(f"\n[{name}]")
        print(f"예상 총비용: {result.build.estimated_group_cost_krw:,}원")
        print(f"점수 입력: {result.build.signals}")
        print(f"적용 가중치: {result.score.weights}")
        print(f"필수조건 통과: {result.score.eligible}")
        print(f"최종 점수: {result.score.total_score}")
        if result.score.rejection_reasons:
            print(f"제외 이유: {', '.join(result.score.rejection_reasons)}")


if __name__ == "__main__":
    main()
