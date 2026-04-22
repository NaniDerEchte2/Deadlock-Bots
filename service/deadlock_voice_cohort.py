from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

MATCH_STATUS_REGEX = re.compile(
    r"\((\d{1,3})\.?\s*min\.?\)",
    re.IGNORECASE,
)


def evaluate_deadlock_presence_row(
    row: Any | None,
    now: int,
    *,
    stale_seconds: int,
) -> tuple[str, int | None, str | None] | None:
    if not row:
        return None

    updated_at = row["deadlock_updated_at"] or row["last_seen_ts"]
    if not updated_at:
        return None
    if now - int(updated_at) > stale_seconds:
        return None

    localized_raw = row["deadlock_localized"] or ""
    localized = str(localized_raw).strip()
    match_info = MATCH_STATUS_REGEX.search(localized)
    deadlock_stage = str(row["deadlock_stage"] or "").strip().lower()
    in_match_now_strict = bool(row["in_match_now_strict"])
    deadlock_minutes_raw = row["deadlock_minutes"]
    server_id_raw = row["last_server_id"] or row["deadlock_party_hint"]
    server_id = str(server_id_raw).strip() if server_id_raw else None

    normalized_minutes: int | None = None
    if deadlock_minutes_raw is not None:
        try:
            normalized_minutes = max(0, int(deadlock_minutes_raw))
        except (TypeError, ValueError):
            normalized_minutes = None

    if in_match_now_strict or deadlock_stage == "match":
        if normalized_minutes is not None:
            return "match", normalized_minutes, server_id
        if match_info:
            try:
                minutes_val = max(0, int(match_info.group(1)))
            except (TypeError, ValueError):
                minutes_val = 0
            return "match", minutes_val, server_id
        return "match", 0, server_id

    if match_info:
        try:
            minutes_val = max(0, int(match_info.group(1)))
        except (TypeError, ValueError):
            minutes_val = 0
        return "match", minutes_val, server_id

    if server_id:
        return "lobby", None, server_id

    return None


def select_best_deadlock_presence(
    steam_ids: Sequence[str],
    presence_map: dict[str, Any],
    now: int,
    *,
    stale_seconds: int,
) -> tuple[str, int | None, str | None, str] | None:
    best: tuple[str, int | None, str | None] | None = None
    best_sid: str | None = None
    best_score = -1

    for sid in steam_ids:
        presence = evaluate_deadlock_presence_row(
            presence_map.get(str(sid)),
            now,
            stale_seconds=stale_seconds,
        )
        if not presence:
            continue
        stage, minutes, server_id = presence
        stage_score = 2 if stage == "match" else 1 if stage == "lobby" else 0
        minutes_score = minutes if minutes is not None else -1
        score = stage_score * 100000 + minutes_score
        if score > best_score:
            best_score = score
            best = (stage, minutes, server_id)
            best_sid = str(sid)

    if best and best_sid:
        return best[0], best[1], best[2], best_sid
    return None


def select_deadlock_channel_cohort(
    entries: Sequence[dict[str, Any]],
    *,
    min_active_players: int,
) -> dict[str, Any] | None:
    if not entries:
        return None

    grouped_members: dict[tuple[str, str], list[int]] = defaultdict(list)
    grouped_minutes: dict[tuple[str, str], list[int]] = defaultdict(list)
    unknown_members: dict[str, list[int]] = defaultdict(list)
    unknown_minutes: dict[str, list[int]] = defaultdict(list)

    for entry in entries:
        stage = str(entry.get("stage") or "")
        if stage not in {"lobby", "match"}:
            continue
        member_id = int(entry["member_id"])
        minutes = int(entry.get("minutes") or 0)
        server_id = entry.get("server_id")
        if server_id:
            key = (stage, str(server_id))
            grouped_members[key].append(member_id)
            grouped_minutes[key].append(minutes)
        else:
            unknown_members[stage].append(member_id)
            unknown_minutes[stage].append(minutes)

    candidate: dict[str, Any] | None = None

    def _consider(
        stage: str, server_id: str | None, member_ids: list[int], minute_values: list[int]
    ) -> None:
        nonlocal candidate
        if len(member_ids) < min_active_players:
            return
        next_candidate = {
            "stage": stage,
            "server_id": server_id,
            "member_ids": list(member_ids),
            "minute_values": list(minute_values),
            "member_count": len(member_ids),
        }
        if candidate is None:
            candidate = next_candidate
            return
        if candidate["stage"] != "match" and stage == "match":
            candidate = next_candidate
            return
        if candidate["stage"] == stage and len(member_ids) > int(candidate["member_count"]):
            candidate = next_candidate

    for (stage, server_id), member_ids in grouped_members.items():
        _consider(stage, server_id, member_ids, grouped_minutes[(stage, server_id)])
    for stage, member_ids in unknown_members.items():
        _consider(stage, None, member_ids, unknown_minutes[stage])

    return candidate
