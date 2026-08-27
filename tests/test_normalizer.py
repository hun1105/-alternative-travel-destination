from __future__ import annotations

import unittest
from datetime import datetime

from plan_b_api import (
    is_open_at,
    normalize_place,
    parse_adult_fee,
    parse_event_date,
)


GYONGBOKGUNG = {
    "content_id": "126508",
    "content_type_id": "12",
    "common": [
        {
            "contentid": "126508",
            "contenttypeid": "12",
            "title": "경복궁",
            "addr1": "서울특별시 종로구 사직로 161",
            "addr2": "",
            "mapx": "126.97672186606306",
            "mapy": "37.576030700049394",
            "overview": "조선왕조 제일의 법궁",
            "homepage": "https://royal.khs.go.kr/",
            "firstimage": "https://example.com/palace.jpg",
            "cpyrhtDivCd": "Type1",
        }
    ],
    "intro": [
        {
            "restdate": "매주 화요일",
            "usetime": (
                "[1월~2월/11월~12월]09:00~17:00 (입장마감 16:00)"
                "[3월~5월/9월~10월]09:00~18:00 (입장마감 17:00)"
                "[6월~8월] 09:00~18:30 (입장마감 17:30)"
            ),
            "parking": "가능 (승용차 240대)",
        }
    ],
    "info": [
        {
            "infoname": "입장료",
            "infotext": (
                "- 개인 대인 3,000원\n"
                "- 단체 대인 2,400원\n"
                "※ 무료 : 만 24세 이하"
            ),
        },
        {"infoname": "화장실", "infotext": "있음"},
    ],
    "images": [],
}

FESTIVAL = {
    "content_id": "2648460",
    "content_type_id": "15",
    "common": [{
        "title": "경복궁 별빛야행",
        "mapx": "126.9767",
        "mapy": "37.5760",
        "overview": "100% 사전예약으로 운영한다.",
        "firstimage": "https://example.com/festival.jpg",
    }],
    "intro": [{
        "eventstartdate": "20260402",
        "eventenddate": "20260517",
        "playtime": "1회 18:40 / 2회 19:40",
        "usetimefestival": "1인 60,000원",
        "bookingplace": "티켓링크 사전 예매",
    }],
    "info": [],
}


class NormalizerTests(unittest.TestCase):
    def test_normalizes_gyeongbokgung_detail(self) -> None:
        place = normalize_place(GYONGBOKGUNG)

        self.assertEqual(place.title, "경복궁")
        self.assertEqual(place.adult_fee_krw, 3000)
        self.assertFalse(place.is_free)
        self.assertEqual(place.closed_weekdays, (1,))
        self.assertEqual(len(place.operating_windows), 3)
        self.assertTrue(place.parking_available)
        self.assertTrue(place.toilet_available)
        self.assertEqual(place.normalization_confidence, 1.0)

    def test_open_state_uses_month_and_weekday(self) -> None:
        place = normalize_place(GYONGBOKGUNG)

        self.assertTrue(is_open_at(place, datetime(2026, 7, 29, 10, 0)))
        self.assertFalse(is_open_at(place, datetime(2026, 7, 28, 10, 0)))
        self.assertFalse(is_open_at(place, datetime(2026, 7, 29, 19, 0)))

    def test_last_admission_time_closes_entry_early(self) -> None:
        place = normalize_place(GYONGBOKGUNG)

        self.assertTrue(is_open_at(place, datetime(2026, 7, 29, 17, 29)))
        self.assertFalse(is_open_at(place, datetime(2026, 7, 29, 17, 30)))

    def test_normalizes_festival_period_sessions_fee_and_booking(self) -> None:
        place = normalize_place(FESTIVAL)

        self.assertEqual(str(place.event_start_date), "2026-04-02")
        self.assertEqual(str(place.event_end_date), "2026-05-17")
        self.assertEqual(place.session_times, ("18:40", "19:40"))
        self.assertEqual(place.adult_fee_krw, 60_000)
        self.assertTrue(place.reservation_required)
        self.assertEqual(place.normalization_confidence, 1.0)
        self.assertFalse(is_open_at(place, datetime(2026, 7, 29, 19)))
        self.assertIsNone(is_open_at(place, datetime(2026, 4, 10, 19)))

    def test_parses_compact_fee_and_flexible_date(self) -> None:
        self.assertEqual(parse_adult_fee("성인 5천원"), (5000, False))
        self.assertEqual(str(parse_event_date("2026.05.17")), "2026-05-17")

    def test_event_ranges_keep_only_session_start_times(self) -> None:
        festival = {
            **FESTIVAL,
            "intro": [{
                **FESTIVAL["intro"][0],
                "playtime": "1회 18:30~20:20 / 2회 19:40~21:30",
            }],
        }
        place = normalize_place(festival)

        self.assertEqual(place.session_times, ("18:30", "19:40"))
        self.assertEqual(len(place.operating_windows), 2)
        self.assertTrue(is_open_at(place, datetime(2026, 4, 10, 20)))
        self.assertFalse(is_open_at(place, datetime(2026, 4, 10, 22)))

    def test_normalizes_restaurant_specific_fields(self) -> None:
        restaurant = {
            "content_id": "food-1",
            "content_type_id": "39",
            "common": [{
                "title": "한식당",
                "mapx": "126.97",
                "mapy": "37.57",
            }],
            "intro": [{
                "opentimefood": "10:00~22:00",
                "restdatefood": "매주 월요일",
                "parkingfood": "가능",
                "reservationfood": "전화 예약 필수",
            }],
            "info": [],
        }
        place = normalize_place(restaurant)

        self.assertEqual(place.raw_operating_hours, "10:00~22:00")
        self.assertEqual(place.closed_weekdays, (0,))
        self.assertTrue(place.parking_available)
        self.assertTrue(place.reservation_required)
        self.assertTrue(is_open_at(place, datetime(2026, 8, 4, 12)))

    def test_normalizes_shopping_specific_fields(self) -> None:
        shopping = {
            "content_id": "shop-1",
            "content_type_id": "38",
            "common": [{
                "title": "전통시장",
                "mapx": "126.97",
                "mapy": "37.57",
            }],
            "intro": [{
                "opentime": "09:00~20:00",
                "restdateshopping": "매주 화요일",
                "parkingshopping": "가능",
            }],
            "info": [],
        }
        place = normalize_place(shopping)

        self.assertEqual(place.raw_operating_hours, "09:00~20:00")
        self.assertEqual(place.closed_weekdays, (1,))
        self.assertTrue(place.parking_available)
        self.assertTrue(is_open_at(place, datetime(2026, 8, 5, 12)))


if __name__ == "__main__":
    unittest.main()
