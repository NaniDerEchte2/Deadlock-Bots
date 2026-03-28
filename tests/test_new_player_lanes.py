from __future__ import annotations

import unittest

from cogs.tempvoice.new_player_lanes import (
    ANCHOR_CHANNEL_ID,
    ManagedLaneSnapshot,
    lane_name_for_index,
    parse_lane_index,
    plan_managed_lanes,
)


class NewPlayerAdaptiveLanesTests(unittest.TestCase):
    def test_parse_lane_index_handles_anchor_and_numbered_lanes(self) -> None:
        self.assertEqual(parse_lane_index(ANCHOR_CHANNEL_ID, "anything"), 1)
        self.assertEqual(parse_lane_index(200, lane_name_for_index(2)), 2)
        self.assertEqual(parse_lane_index(300, lane_name_for_index(7)), 7)
        self.assertIsNone(parse_lane_index(400, lane_name_for_index(1)))
        self.assertIsNone(parse_lane_index(500, "Andere Lane 2"))

    def test_plan_creates_spare_lane_when_anchor_is_full(self) -> None:
        plan = plan_managed_lanes(anchor_member_count=6, extra_snapshots=[])

        self.assertEqual(plan.reassignments, ())
        self.assertEqual(plan.delete_ids, ())
        self.assertEqual(plan.create_indices, (2,))

    def test_plan_deletes_empty_tail_when_anchor_is_not_full(self) -> None:
        plan = plan_managed_lanes(
            anchor_member_count=4,
            extra_snapshots=[ManagedLaneSnapshot(channel_id=201, current_index=2, member_count=0)],
        )

        self.assertEqual(plan.reassignments, ())
        self.assertEqual(plan.delete_ids, (201,))
        self.assertEqual(plan.create_indices, ())

    def test_plan_compacts_occupied_lane_down_and_removes_gap(self) -> None:
        plan = plan_managed_lanes(
            anchor_member_count=3,
            extra_snapshots=[
                ManagedLaneSnapshot(channel_id=202, current_index=2, member_count=0),
                ManagedLaneSnapshot(channel_id=203, current_index=3, member_count=2),
            ],
        )

        self.assertEqual(plan.reassignments, ((203, 2),))
        self.assertEqual(plan.delete_ids, (202,))
        self.assertEqual(plan.create_indices, ())

    def test_plan_keeps_single_spare_after_highest_full_lane(self) -> None:
        plan = plan_managed_lanes(
            anchor_member_count=8,
            extra_snapshots=[
                ManagedLaneSnapshot(channel_id=302, current_index=2, member_count=6),
                ManagedLaneSnapshot(channel_id=303, current_index=3, member_count=0),
                ManagedLaneSnapshot(channel_id=304, current_index=4, member_count=0),
            ],
        )

        self.assertEqual(plan.reassignments, ((302, 2), (303, 3)))
        self.assertEqual(plan.delete_ids, (304,))
        self.assertEqual(plan.create_indices, ())


if __name__ == "__main__":
    unittest.main()
