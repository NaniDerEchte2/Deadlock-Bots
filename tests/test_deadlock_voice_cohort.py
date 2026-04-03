from __future__ import annotations

import unittest

from service.deadlock_voice_cohort import (
    evaluate_deadlock_presence_row,
    select_best_deadlock_presence,
    select_deadlock_channel_cohort,
)


class DeadlockVoiceCohortTests(unittest.TestCase):
    def test_evaluate_deadlock_presence_row_detects_match_and_staleness(self) -> None:
        row = {
            "deadlock_updated_at": 1_000,
            "last_seen_ts": None,
            "deadlock_localized": "{deadlock:ranked} queue text (18 min.)",
            "deadlock_stage": "match",
            "in_match_now_strict": 1,
            "deadlock_minutes": 18,
            "last_server_id": "srv-1",
            "deadlock_party_hint": None,
        }

        self.assertEqual(
            evaluate_deadlock_presence_row(row, 1_050, stale_seconds=180),
            ("match", 18, "srv-1"),
        )
        self.assertIsNone(evaluate_deadlock_presence_row(row, 1_500, stale_seconds=180))

    def test_select_best_deadlock_presence_prefers_match(self) -> None:
        presence_map = {
            "steam-lobby": {
                "deadlock_updated_at": 1_000,
                "last_seen_ts": None,
                "deadlock_localized": "",
                "deadlock_stage": "lobby",
                "in_match_now_strict": 0,
                "deadlock_minutes": None,
                "last_server_id": "lobby-1",
                "deadlock_party_hint": None,
            },
            "steam-match": {
                "deadlock_updated_at": 1_000,
                "last_seen_ts": None,
                "deadlock_localized": "{deadlock:game} something (22 min.)",
                "deadlock_stage": "match",
                "in_match_now_strict": 1,
                "deadlock_minutes": 22,
                "last_server_id": "match-1",
                "deadlock_party_hint": None,
            },
        }

        self.assertEqual(
            select_best_deadlock_presence(
                ["steam-lobby", "steam-match"],
                presence_map,
                1_020,
                stale_seconds=180,
            ),
            ("match", 22, "match-1", "steam-match"),
        )

    def test_select_deadlock_channel_cohort_groups_by_same_server(self) -> None:
        entries = [
            {"member_id": 1, "stage": "lobby", "minutes": 0, "server_id": "srv-a"},
            {"member_id": 2, "stage": "lobby", "minutes": 0, "server_id": "srv-a"},
            {"member_id": 3, "stage": "lobby", "minutes": 0, "server_id": "srv-b"},
            {"member_id": 4, "stage": "match", "minutes": 12, "server_id": "srv-c"},
        ]

        cohort = select_deadlock_channel_cohort(entries, min_active_players=1)

        self.assertIsNotNone(cohort)
        self.assertEqual(cohort["stage"], "match")
        self.assertEqual(cohort["member_ids"], [4])

        lobby_only = select_deadlock_channel_cohort(entries[:3], min_active_players=1)
        self.assertIsNotNone(lobby_only)
        self.assertEqual(lobby_only["stage"], "lobby")
        self.assertEqual(lobby_only["server_id"], "srv-a")
        self.assertEqual(lobby_only["member_ids"], [1, 2])

    def test_evaluate_deadlock_presence_row_uses_strict_match_without_minutes_string(self) -> None:
        row = {
            "deadlock_updated_at": 2_000,
            "last_seen_ts": None,
            "deadlock_localized": "{deadlock:hero} warmup text",
            "deadlock_stage": "match",
            "in_match_now_strict": 1,
            "deadlock_minutes": 0,
            "last_server_id": "srv-2",
            "deadlock_party_hint": None,
        }

        self.assertEqual(
            evaluate_deadlock_presence_row(row, 2_030, stale_seconds=180),
            ("match", 0, "srv-2"),
        )


if __name__ == "__main__":
    unittest.main()
