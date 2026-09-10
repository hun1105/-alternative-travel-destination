from __future__ import annotations

import unittest

from plan_b_api.api_service import PlanBApiService
from plan_b_api.route_optimizer import (
    haversine_distance_meters,
    optimize_schedule_order,
    total_route_distance,
)


class TestRouteOptimizer(unittest.TestCase):
    def setUp(self) -> None:
        self.zigzag_stops = [
            {'item_id': 'stop-0', 'place': {'name': 'A', 'longitude': 126.97, 'latitude': 37.56}},
            {'item_id': 'stop-1', 'place': {'name': 'B', 'longitude': 127.10, 'latitude': 37.56}},
            {'item_id': 'stop-2', 'place': {'name': 'C', 'longitude': 127.01, 'latitude': 37.56}},
            {'item_id': 'stop-3', 'place': {'name': 'D', 'longitude': 127.05, 'latitude': 37.56}},
        ]

    def test_haversine_distance(self) -> None:
        dist = haversine_distance_meters(126.9767, 37.5760, 126.9910, 37.5794)
        self.assertGreater(dist, 1000)
        self.assertLess(dist, 2000)

    def test_too_few_items(self) -> None:
        items = self.zigzag_stops[:2]
        res = optimize_schedule_order(items)
        self.assertFalse(res.is_improved)
        self.assertEqual(res.optimized_indices, [0, 1])
        self.assertEqual(res.saved_distance_meters, 0.0)

    def test_zigzag_optimization_keep_first(self) -> None:
        res = optimize_schedule_order(self.zigzag_stops, keep_first=True)
        self.assertTrue(res.is_improved)
        self.assertEqual(res.optimized_indices, [0, 2, 3, 1])
        self.assertGreater(res.saved_distance_meters, 5000)
        self.assertGreater(res.estimated_saved_minutes, 10)
        self.assertEqual([item['place']['name'] for item in res.items], ['A', 'C', 'D', 'B'])

    def test_zigzag_with_locked_item(self) -> None:
        items = [
            dict(self.zigzag_stops[0]),
            {**self.zigzag_stops[1], 'locked': True},
            dict(self.zigzag_stops[2]),
            dict(self.zigzag_stops[3]),
        ]
        res = optimize_schedule_order(items, keep_first=True)
        self.assertEqual(res.optimized_indices[1], 1)
        self.assertEqual(res.items[1]['place']['name'], 'B')

    def test_fixed_arrival_time_treated_as_locked(self) -> None:
        items = [
            dict(self.zigzag_stops[0]),
            dict(self.zigzag_stops[1]),
            {**self.zigzag_stops[2], 'fixed_arrival_time': '14:00'},
            dict(self.zigzag_stops[3]),
        ]
        res = optimize_schedule_order(items, keep_first=True)
        self.assertEqual(res.optimized_indices[2], 2)
        self.assertEqual(res.items[2]['place']['name'], 'C')

    def test_optimize_without_keep_first(self) -> None:
        stops = [
            {'item_id': 'stop-1', 'place': {'name': 'B', 'longitude': 127.10, 'latitude': 37.56}},
            {'item_id': 'stop-0', 'place': {'name': 'A', 'longitude': 126.97, 'latitude': 37.56}},
            {'item_id': 'stop-2', 'place': {'name': 'C', 'longitude': 127.01, 'latitude': 37.56}},
            {'item_id': 'stop-3', 'place': {'name': 'D', 'longitude': 127.05, 'latitude': 37.56}},
        ]
        res = optimize_schedule_order(stops, keep_first=False)
        self.assertTrue(res.is_improved)
        names = [item['place']['name'] for item in res.items]
        self.assertTrue(names in (['A', 'C', 'D', 'B'], ['B', 'D', 'C', 'A']))

    def test_api_service_optimize_schedule(self) -> None:
        body = {
            'items': self.zigzag_stops,
            'keep_first': True,
            'average_speed_kmh': 30.0,
        }
        data = PlanBApiService.optimize_schedule(body)
        self.assertTrue(data['is_improved'])
        self.assertEqual(data['optimized_indices'], [0, 2, 3, 1])
        self.assertGreater(data['saved_distance_meters'], 5000)
        self.assertEqual(len(data['items']), 4)

    def test_api_service_invalid_items(self) -> None:
        with self.assertRaises(ValueError):
            PlanBApiService.optimize_schedule({'items': 'invalid'})


if __name__ == '__main__':
    unittest.main()
