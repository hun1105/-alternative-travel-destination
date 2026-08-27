"""사용자 우선순위에 따라 후보 순위가 바뀌는 예제."""

from plan_b_api import CandidateSignals, UserPriorities, score_candidate


near_place = CandidateSignals(
    weather_fit=0.8,
    route_time=1.0,
    crowd_avoidance=0.2,
    budget_fit=0.5,
    child_fit=0.8,
    walking_fit=0.8,
    data_confidence=0.9,
)

quiet_place = CandidateSignals(
    weather_fit=0.8,
    route_time=0.2,
    crowd_avoidance=1.0,
    budget_fit=0.5,
    child_fit=0.8,
    walking_fit=0.8,
    data_confidence=0.9,
)


def show(label: str, priorities: UserPriorities) -> None:
    near = score_candidate(near_place, priorities)
    quiet = score_candidate(quiet_place, priorities)
    winner = "가까운 장소" if near.total_score > quiet.total_score else "한적한 장소"

    print(f"\n[{label}]")
    print(f"가까운 장소: {near.total_score}")
    print(f"한적한 장소: {quiet.total_score}")
    print(f"추천 1위: {winner}")


show(
    "이동시간 최우선 사용자",
    UserPriorities.from_order("route_time"),
)
show(
    "혼잡 회피 최우선 사용자",
    UserPriorities.from_order("crowd_avoidance"),
)
