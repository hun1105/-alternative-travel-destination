"""대체 후보 방문 후 다음 고정 일정 도착 가능성 계산."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class NextScheduleConstraint:
    longitude: float
    latitude: float
    arrival_deadline: datetime
    visit_minutes: float = 60.0
    buffer_minutes: float = 10.0
    title: str = "다음 일정"

    def validate(self, start_time: datetime) -> None:
        if self.arrival_deadline <= start_time:
            raise ValueError("다음 일정 도착시각은 추천 시작시각보다 늦어야 합니다.")
        if self.visit_minutes < 0 or self.buffer_minutes < 0:
            raise ValueError("체류시간과 여유시간은 0 이상이어야 합니다.")


@dataclass(frozen=True)
class ScheduleFeasibility:
    feasible: bool
    next_schedule_title: str
    deadline: datetime
    candidate_arrival: datetime
    candidate_departure: datetime
    estimated_next_arrival: datetime
    inbound_minutes: float
    visit_minutes: float
    onward_minutes: float
    buffer_minutes: float
    required_minutes: float
    available_minutes: float
    slack_minutes: float
    onward_mode: str
    onward_source: str


def evaluate_schedule_feasibility(
    *,
    start_time: datetime,
    constraint: NextScheduleConstraint,
    inbound_minutes: float,
    onward_minutes: float,
    onward_mode: str,
    onward_source: str,
    route_policy_ok: bool = True,
) -> ScheduleFeasibility:
    constraint.validate(start_time)
    if inbound_minutes < 0 or onward_minutes < 0:
        raise ValueError("이동시간은 0 이상이어야 합니다.")

    candidate_arrival = start_time + timedelta(minutes=inbound_minutes)
    candidate_departure = candidate_arrival + timedelta(
        minutes=constraint.visit_minutes
    )
    estimated_next_arrival = candidate_departure + timedelta(
        minutes=onward_minutes
    )
    available = (constraint.arrival_deadline - start_time).total_seconds() / 60
    required = (
        inbound_minutes
        + constraint.visit_minutes
        + onward_minutes
        + constraint.buffer_minutes
    )
    slack = available - required
    return ScheduleFeasibility(
        feasible=route_policy_ok and slack >= 0,
        next_schedule_title=constraint.title,
        deadline=constraint.arrival_deadline,
        candidate_arrival=candidate_arrival,
        candidate_departure=candidate_departure,
        estimated_next_arrival=estimated_next_arrival,
        inbound_minutes=round(inbound_minutes, 2),
        visit_minutes=round(constraint.visit_minutes, 2),
        onward_minutes=round(onward_minutes, 2),
        buffer_minutes=round(constraint.buffer_minutes, 2),
        required_minutes=round(required, 2),
        available_minutes=round(available, 2),
        slack_minutes=round(slack, 2),
        onward_mode=onward_mode,
        onward_source=onward_source,
    )
