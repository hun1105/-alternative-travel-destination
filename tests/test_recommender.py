from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime

from plan_b_api import (
    KTOResponse,
    NormalizedPlace,
    TripContext,
    UserPriorities,
    build_candidate_evidence,
    facts_from_tourapi,
    estimate_indoor_ratio,
    normalize_place,
    recommend_nearby,
)
from plan_b_api.recommender import RankedTourCandidate, apply_confidence_penalty
from plan_b_api.signal_builder import evaluate_place_candidate


def bundle(content_id: str, type_id: str, title: str) -> dict[str, object]:
    return {
        "content_id": content_id,
        "content_type_id": type_id,
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
            {"infoname": "입장료", "infotext": "대인 3,000원"},
            {"infoname": "화장실", "infotext": "있음"},
        ],
    }


class FakeClient:
    def location_based_list(self, **_: object) -> KTOResponse:
        items = [
            {
                "contentid": "1",
                "contenttypeid": "12",
                "title": "야외 공원",
                "dist": "300",
            },
            {
                "contentid": "2",
                "contenttypeid": "14",
                "title": "실내 박물관",
                "dist": "600",
            },
            {
                "contentid": "3",
                "contenttypeid": "39",
                "title": "주변 음식점",
                "dist": "100",
            },
            {
                "contentid": "4",
                "contenttypeid": "38",
                "title": "주변 쇼핑몰",
                "dist": "200",
            },
            {
                "contentid": "5",
                "contenttypeid": "32",
                "title": "제외 숙박시설",
                "dist": "50",
            },
            {"title": "ID 없는 후보"},
        ]
        return KTOResponse("locationBasedList2", items, 6, 1, 6, {})

    def detail_bundle(
        self,
        content_id: str,
        content_type_id: str,
        *,
        include_images: bool,
    ) -> dict[str, object]:
        titles = {
            "1": "야외 공원",
            "2": "실내 박물관",
            "3": "주변 음식점",
            "4": "주변 쇼핑몰",
        }
        title = titles[content_id]
        return bundle(content_id, content_type_id, title)


class RecommenderTests(unittest.TestCase):
    def test_low_confidence_candidate_receives_larger_penalty(self) -> None:
        high_place = normalize_place(bundle("2", "14", "실내 박물관"))
        low_place = replace(high_place, normalization_confidence=0.2)
        candidate_facts, notes = facts_from_tourapi({"dist": "300"}, high_place)
        trip = TripContext(arrival_time=datetime(2026, 7, 29, 14))
        priorities = UserPriorities.from_order("route_time")

        def scored(place: NormalizedPlace) -> RankedTourCandidate:
            evaluation = evaluate_place_candidate(
                place, candidate_facts, trip, priorities
            )
            return apply_confidence_penalty(RankedTourCandidate(
                title="실내 박물관",
                content_id="2",
                content_type_id="14",
                distance_meters=300,
                place=place,
                facts=candidate_facts,
                evaluation=evaluation,
                estimation_notes=notes,
            ))

        high = scored(high_place)
        low = scored(low_place)
        self.assertGreater(low.confidence_penalty, high.confidence_penalty)
        self.assertLess(low.evaluation.score.total_score, high.evaluation.score.total_score)

    def test_tourapi_facts_use_real_distance(self) -> None:
        place = normalize_place(bundle("2", "14", "실내 박물관"))
        facts, notes = facts_from_tourapi({"dist": "750"}, place)

        self.assertEqual(facts.walking_meters, 750.0)
        self.assertEqual(facts.route_minutes, 10.0)
        self.assertEqual(facts.indoor_ratio, 0.95)
        self.assertTrue(any("직선거리" in note for note in notes))

    def test_outdoor_title_overrides_museum_word_in_overview(self) -> None:
        place = normalize_place({
            **bundle("1", "12", "건청궁"),
            "common": [{
                "title": "건청궁",
                "mapx": "126.97",
                "mapy": "37.57",
                "overview": "인근에 국립고궁박물관이 위치한다.",
                "firstimage": "https://example.com/image.jpg",
            }],
        })

        self.assertEqual(estimate_indoor_ratio(place), 0.15)

    def test_actual_candidates_are_normalized_scored_and_sorted(self) -> None:
        result = recommend_nearby(
            FakeClient(),  # type: ignore[arg-type]
            map_x=126.97,
            map_y=37.57,
            radius=2000,
            rows=3,
            trip=TripContext(
                arrival_time=datetime(2026, 7, 29, 14),
                weather_severity=0.9,
                remaining_budget_krw=10_000,
            ),
            priorities=UserPriorities.from_order("weather_fit"),
        )

        titles = {candidate.title for candidate in result.candidates}
        self.assertNotIn("주변 음식점", titles)
        self.assertIn("주변 쇼핑몰", titles)
        scores = [
            candidate.evaluation.score.total_score
            for candidate in result.candidates
        ]
        self.assertEqual(scores, sorted(scores, reverse=True))
        park = next(
            candidate for candidate in result.candidates
            if candidate.title == "야외 공원"
        )
        self.assertFalse(park.evaluation.score.eligible)
        self.assertTrue(any("업종(32)" in reason for reason in result.skipped))
        self.assertTrue(any("콘텐츠 ID" in reason for reason in result.skipped))

        museum = next(
            candidate for candidate in result.candidates
            if candidate.title == "실내 박물관"
        )
        evidence = build_candidate_evidence(museum)
        self.assertEqual(evidence.operation_status, "도착 시 운영 확인")
        self.assertIn("예산 적합", evidence.excluded_data)
        self.assertIn("혼잡 회피", evidence.neutral_data)
        self.assertIn("아동 적합", evidence.excluded_data)
        self.assertGreaterEqual(evidence.confidence_percent, 80)

    def test_shopping_included_by_default_restaurant_only_with_flag(self) -> None:
        common = {
            "map_x": 126.97,
            "map_y": 37.57,
            "radius": 2000,
            "rows": 4,
            "trip": TripContext(arrival_time=datetime(2026, 7, 29, 14)),
            "priorities": UserPriorities.from_order("route_time"),
        }
        default_result = recommend_nearby(
            FakeClient(),  # type: ignore[arg-type]
            **common,
        )
        default_titles = {c.title for c in default_result.candidates}
        self.assertNotIn("주변 음식점", default_titles)
        self.assertIn("주변 쇼핑몰", default_titles)

        with_restaurants = recommend_nearby(
            FakeClient(),  # type: ignore[arg-type]
            include_restaurants=True,
            **common,
        )
        with_restaurants_titles = {c.title for c in with_restaurants.candidates}
        self.assertIn("주변 음식점", with_restaurants_titles)


if __name__ == "__main__":
    unittest.main()
