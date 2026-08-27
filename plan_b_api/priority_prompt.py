"""터미널에서 사용자 우선순위를 선택받는다."""

from __future__ import annotations

import re
from collections.abc import Callable

from .scoring import LABELS, PRIORITY_FIELDS, UserPriorities


def prompt_user_priorities(
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> UserPriorities:
    """번호를 높은 순서대로 입력받아 부분 우선순위를 생성한다."""

    output_func(
        "다음 중 여행 중 우선시 하는 요소를 선택해주세요 "
        "(선택 가능한 개수는 1~5개입니다.)"
    )
    for number, field in enumerate(PRIORITY_FIELDS, start=1):
        output_func(f"{number}. {LABELS[field]}")

    while True:
        raw = input_func(
            "우선순위가 높은 순서대로 번호를 입력하세요 "
            "(예: 5,1,3): "
        ).strip()
        tokens = [token for token in re.split(r"[\s,]+", raw) if token]
        try:
            numbers = [int(token) for token in tokens]
        except ValueError:
            output_func("숫자만 입력해주세요.")
            continue

        if not 1 <= len(numbers) <= 5:
            output_func("1~5개를 선택해주세요.")
            continue
        if any(number not in range(1, 6) for number in numbers):
            output_func("항목 번호는 1~5입니다.")
            continue
        if len(numbers) != len(set(numbers)):
            output_func("같은 항목을 중복 선택할 수 없습니다.")
            continue

        fields = tuple(PRIORITY_FIELDS[number - 1] for number in numbers)
        return UserPriorities.from_order(*fields)
