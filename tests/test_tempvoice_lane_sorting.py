from __future__ import annotations

import unittest

from cogs.tempvoice.lane_sorting import LaneSortSnapshot, parse_rank_label, plan_lane_reorder


class TempVoiceLaneSortingTests(unittest.TestCase):
    def test_parse_rank_label_handles_unknown_and_subrank(self) -> None:
        self.assertEqual(parse_rank_label(None), (0, 0))
        self.assertEqual(parse_rank_label("Chill"), (0, 0))
        self.assertEqual(parse_rank_label("Ascendant 3"), (10, 3))
        self.assertEqual(parse_rank_label("Oracle • ab Emissary"), (8, 0))

    def test_plan_lane_reorder_sorts_unknown_low_to_high(self) -> None:
        entries = [
            LaneSortSnapshot(lane_id=101, current_position=4, rank_index=8, subrank=0, stable_order=4),
            LaneSortSnapshot(lane_id=102, current_position=5, rank_index=0, subrank=0, stable_order=5),
            LaneSortSnapshot(lane_id=103, current_position=6, rank_index=2, subrank=0, stable_order=6),
        ]

        moves = plan_lane_reorder(entries)

        self.assertEqual(moves, [(102, 4), (103, 5), (101, 6)])

    def test_plan_lane_reorder_uses_subrank_within_same_major_rank(self) -> None:
        entries = [
            LaneSortSnapshot(lane_id=201, current_position=10, rank_index=10, subrank=6, stable_order=10),
            LaneSortSnapshot(lane_id=202, current_position=11, rank_index=10, subrank=1, stable_order=11),
            LaneSortSnapshot(lane_id=203, current_position=12, rank_index=10, subrank=3, stable_order=12),
        ]

        moves = plan_lane_reorder(entries)

        self.assertEqual(moves, [(202, 10), (203, 11), (201, 12)])

    def test_plan_lane_reorder_keeps_stable_order_for_equal_rank(self) -> None:
        entries = [
            LaneSortSnapshot(lane_id=301, current_position=20, rank_index=6, subrank=0, stable_order=20),
            LaneSortSnapshot(lane_id=302, current_position=21, rank_index=6, subrank=0, stable_order=21),
        ]

        moves = plan_lane_reorder(entries)

        self.assertEqual(moves, [])


if __name__ == "__main__":
    unittest.main()
