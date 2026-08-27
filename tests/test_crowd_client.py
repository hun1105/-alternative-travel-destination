from __future__ import annotations

import json
import unittest

from plan_b_api import (
    CrowdPlace,
    SKCrowdClient,
    SKCrowdConfig,
    match_crowd_place,
)


class CrowdClientTests(unittest.TestCase):
    def test_supported_places_and_realtime_are_parsed(self) -> None:
        def transport(url: str, _: dict[str, str], __: float) -> tuple[int, bytes]:
            if "meta/pois" in url:
                payload = {
                    "status": {"code": "00", "message": "success"},
                    "contents": [{"poiId": "1172091", "poiName": "타임스퀘어"}],
                }
            else:
                payload = {
                    "status": {"code": "00", "message": "success"},
                    "contents": {
                        "poiId": "1172091",
                        "poiName": "타임스퀘어",
                        "rltm": [
                            {
                                "type": 1,
                                "congestion": 0.08,
                                "congestionLevel": 3,
                                "datetime": "20260803150000",
                            }
                        ],
                    },
                }
            return 200, json.dumps(payload).encode()

        client = SKCrowdClient(
            SKCrowdConfig("secret", max_retries=0), transport=transport
        )
        self.assertEqual(client.supported_places()[0].name, "타임스퀘어")
        snapshot = client.realtime("1172091")
        self.assertEqual(snapshot.label, "혼잡")
        self.assertAlmostEqual(snapshot.normalized_level, 2 / 3)

    def test_name_matching_ignores_spaces_and_symbols_only(self) -> None:
        places = (CrowdPlace("1", "더현대 서울"),)
        self.assertEqual(match_crowd_place("더현대서울", places).poi_id, "1")
        self.assertIsNone(match_crowd_place("더현대서울 전시장", places))


if __name__ == "__main__":
    unittest.main()
