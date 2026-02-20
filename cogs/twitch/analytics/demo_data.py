"""
Demo data for the public Analytics Dashboard.

Fake streamer: deadlock_de_demo
Profile: mid-tier German Deadlock streamer, 4x/week, avg ~380 viewers, ~8k followers.
All data is synthetic and does not reflect any real streamer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

DEMO_STREAMER = "deadlock_de_demo"
DEMO_DISPLAY_NAME = "Deadlock_DE_Demo"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _date(offset_days: int = 0) -> str:
    """Return ISO date string relative to today."""
    d = (datetime.now(timezone.utc) - timedelta(days=offset_days)).date()
    return d.isoformat()


def _dt(offset_days: int = 0, hour: int = 19) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=offset_days)
    d = d.replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.isoformat()


# ---------------------------------------------------------------------------
# Per-endpoint demo data factories
# ---------------------------------------------------------------------------


def get_auth_status() -> Dict[str, Any]:
    return {
        "authenticated": True,
        "level": "partner",
        "isAdmin": False,
        "isLocalhost": False,
        "canViewAllStreamers": False,
        "twitchLogin": DEMO_STREAMER,
        "displayName": DEMO_DISPLAY_NAME,
        "demoMode": True,
        "permissions": {
            "viewAllStreamers": False,
            "viewComparison": True,
            "viewChatAnalytics": True,
            "viewOverlap": True,
        },
    }


def get_streamers() -> List[Dict[str, Any]]:
    return [{"login": DEMO_STREAMER, "isPartner": True}]


def get_overview(days: int = 30) -> Dict[str, Any]:
    return {
        "streamer": DEMO_STREAMER,
        "days": days,
        "scores": {
            "total": 72,
            "reach": 68,
            "retention": 78,
            "engagement": 74,
            "growth": 65,
            "monetization": 55,
            "network": 71,
        },
        "summary": {
            "avgViewers": 382,
            "peakViewers": 1087,
            "totalHoursWatched": 5342,
            "totalAirtime": 14.0,
            "followersDelta": 167,
            "followersGained": 183,
            "followersPerHour": 11.9,
            "followersGainedPerHour": 13.1,
            "retention10m": 71.2,
            "retentionReliable": True,
            "uniqueChatters": 634,
            "streamCount": 14,
            "avgViewersTrend": 6.8,
            "peakViewersTrend": 3.2,
            "followersTrend": 12.4,
            "retentionTrend": 1.9,
        },
        "sessions": _get_sessions(),
        "findings": [
            {
                "type": "pos",
                "title": "Starke Retention",
                "text": "71 % deiner Zuschauer bleiben die ersten 10 Minuten – Top 25 % der Kategorie.",
            },
            {
                "type": "pos",
                "title": "Stetiges Wachstum",
                "text": "Follower-Wachstum +12 % vs. Vormonat. Du gewinnst ~13 Follower pro Stream-Stunde.",
            },
            {
                "type": "warn",
                "title": "Späte Starts",
                "text": "Freitag-Streams starten oft nach 21:00 Uhr – die Kategorie hat dann weniger Zuschauer.",
            },
            {
                "type": "info",
                "title": "Raid-Netzwerk aktiv",
                "text": "Du hast in 30 Tagen 8 Raids gesendet und 5 empfangen.",
            },
            {
                "type": "neg",
                "title": "Wenig Samstag-Streams",
                "text": "Samstag 17:00–20:00 Uhr hat die höchste Viewer-Dichte – nur 2 Streams in dem Slot.",
            },
        ],
        "actions": [
            {
                "tag": "Schedule",
                "text": "Teste Samstag 17:00 Uhr als festen Startslot",
                "priority": "high",
            },
            {
                "tag": "Retention",
                "text": "Intro-Phase kürzen: Viele droppen in Minute 3–5",
                "priority": "medium",
            },
            {
                "tag": "Titel",
                "text": "Variiere Titel öfter – 60 % der Streams hatten identische Titel",
                "priority": "low",
            },
        ],
        "correlations": {"durationVsViewers": 0.42, "chatVsRetention": 0.61},
        "network": {"sent": 8, "received": 5, "sentViewers": 2840},
        "categoryRank": 12,
        "categoryTotal": 58,
        "audienceInsights": {
            "watchTimeDistribution": _watch_time_distribution(),
            "followerFunnel": _follower_funnel(),
            "tagPerformance": _tag_analysis_extended(),
            "titlePerformance": _title_performance(),
            "trends": {
                "watchTimeChange": 8.4,
                "conversionChange": 2.1,
                "viewerReturnRate": 38.2,
                "viewerReturnChange": 4.7,
            },
        },
    }


def _get_sessions() -> List[Dict[str, Any]]:
    sessions = []
    # 14 sessions over past 30 days, streaming Mon/Wed/Fri/Sat
    stream_days = [2, 4, 5, 7, 9, 11, 12, 14, 16, 18, 19, 21, 23, 25]
    viewer_bases = [
        410,
        390,
        365,
        420,
        375,
        395,
        445,
        360,
        385,
        400,
        430,
        370,
        355,
        410,
    ]
    peaks = [1020, 890, 780, 1087, 820, 940, 1050, 730, 860, 920, 1010, 790, 720, 960]
    durations = [
        13200,
        10800,
        14400,
        12600,
        10800,
        13500,
        14400,
        9000,
        12000,
        12600,
        13800,
        10800,
        9600,
        13200,
    ]
    followers_start = [
        8283,
        8297,
        8310,
        8323,
        8338,
        8351,
        8363,
        8379,
        8391,
        8402,
        8415,
        8430,
        8441,
        8454,
    ]

    titles = [
        "Deadlock Ranked Grind | Platin → Diamond",
        "Deadlock Ranked Grind | Platin → Diamond",
        "Ranked mit Freunden | !discord",
        "Deadlock Solo Q | Road to Diamond",
        "Deadlock Ranked Grind | Platin → Diamond",
        "Neue Patch-Analyse + Ranked | !guide",
        "Ranked mit Zuschauern | Komm rein!",
        "Deadlock Solo Q | Road to Diamond",
        "Deadlock Ranked Grind | Platin → Diamond",
        "Ranked mit Freunden | !discord",
        "Neue Patch-Analyse + Ranked | !guide",
        "Deadlock Solo Q | Road to Diamond",
        "Ranked mit Zuschauern | Komm rein!",
        "Deadlock Ranked Grind | Platin → Diamond",
    ]

    for i, (offset, base, peak, dur, fs, title) in enumerate(
        zip(stream_days, viewer_bases, peaks, durations, followers_start, titles)
    ):
        chatters = int(base * 0.13 + 18)
        sessions.append(
            {
                "id": 1000 + i,
                "date": _date(offset),
                "startTime": _dt(offset, hour=19 + (i % 2)),
                "duration": dur,
                "startViewers": int(base * 0.7),
                "peakViewers": peak,
                "endViewers": int(base * 0.55),
                "avgViewers": base,
                "retention5m": round(83 - i * 0.4, 1),
                "retention10m": round(71 - i * 0.3, 1),
                "retention20m": round(58 - i * 0.2, 1),
                "dropoffPct": round(17 + i * 0.3, 1),
                "uniqueChatters": chatters,
                "firstTimeChatters": int(chatters * 0.28),
                "returningChatters": int(chatters * 0.72),
                "followersStart": fs,
                "followersEnd": fs + 11 + (i % 5),
                "title": title,
            }
        )
    return sessions


def get_monthly_stats() -> List[Dict[str, Any]]:
    months = [
        ("März", 3, 2025, 8, 230, 640, 92, 8, 280),
        ("April", 4, 2025, 10, 280, 790, 104, 10, 305),
        ("Mai", 5, 2025, 10, 310, 860, 112, 12, 322),
        ("Juni", 6, 2025, 11, 340, 940, 124, 14, 335),
        ("Juli", 7, 2025, 12, 360, 980, 128, 15, 348),
        ("Aug", 8, 2025, 11, 375, 1020, 126, 14, 355),
        ("Sep", 9, 2025, 13, 368, 1010, 130, 15, 362),
        ("Okt", 10, 2025, 12, 372, 1040, 128, 14, 368),
        ("Nov", 11, 2025, 13, 378, 1060, 134, 15, 374),
        ("Dez", 12, 2025, 10, 371, 1020, 118, 13, 370),
        ("Jan", 1, 2026, 13, 379, 1070, 136, 16, 381),
        ("Feb", 2, 2026, 14, 382, 1087, 140, 17, 390),
    ]
    result = []
    for label, month, year, streams, avg, peak, airtime, followers, chatters in months:
        result.append(
            {
                "year": year,
                "month": month,
                "monthLabel": label,
                "totalHoursWatched": round(avg * airtime, 0),
                "totalAirtime": airtime,
                "avgViewers": avg,
                "peakViewers": peak,
                "followerDelta": followers,
                "uniqueChatters": chatters,
                "streamCount": streams,
            }
        )
    return result


def get_weekday_stats() -> List[Dict[str, Any]]:
    data = [
        (0, "Sonntag", 2, 2.5, 310, 650, 28),
        (1, "Montag", 3, 3.5, 372, 890, 42),
        (2, "Dienstag", 0, 0.0, 0, 0, 0),
        (3, "Mittwoch", 4, 3.2, 388, 940, 46),
        (4, "Donnerstag", 1, 2.8, 345, 780, 38),
        (5, "Freitag", 3, 3.8, 401, 1020, 48),
        (6, "Samstag", 2, 3.1, 423, 1087, 51),
    ]
    return [
        {
            "weekday": wd,
            "weekdayLabel": lbl,
            "streamCount": sc,
            "avgHours": ah,
            "avgViewers": av,
            "avgPeak": ap,
            "totalFollowers": tf,
        }
        for wd, lbl, sc, ah, av, ap, tf in data
    ]


def get_hourly_heatmap() -> List[Dict[str, Any]]:
    rows = []
    for wd in range(7):
        for h in range(24):
            if 18 <= h <= 22 and wd in (1, 3, 5, 6):
                sc = 2 if wd in (5, 6) else 1
                av = 382 + (h - 18) * 15 - wd * 5
                ap = av + 180
            else:
                sc = 0
                av = 0
                ap = 0
            rows.append(
                {
                    "weekday": wd,
                    "hour": h,
                    "streamCount": sc,
                    "avgViewers": max(0, av),
                    "avgPeak": max(0, ap),
                }
            )
    return rows


def get_calendar_heatmap() -> List[Dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    rows = []
    base_date = today - timedelta(days=364)
    stream_value = 0
    for i in range(365):
        d = base_date + timedelta(days=i)
        wd = d.weekday()  # 0=Mon
        # Stream days: Mon(0), Wed(2), Fri(4), Sat(5)
        if wd in (0, 2, 4, 5) and i > 0:
            hours = round(2.8 + (hash(str(d)) % 20) / 10, 1)
            avg_v = 380 + (hash(str(d)) % 80) - 40
            stream_value = round(avg_v * hours, 0)
            sc = 1
        else:
            stream_value = 0
            hours = 0.0
            sc = 0
        rows.append(
            {
                "date": d.isoformat(),
                "value": stream_value,
                "streamCount": sc,
                "hoursWatched": hours,
            }
        )
    return rows


def get_chat_analytics() -> Dict[str, Any]:
    return {
        "totalMessages": 84320,
        "uniqueChatters": 634,
        "firstTimeChatters": 178,
        "returningChatters": 456,
        "messagesPerMinute": 14.2,
        "chatterReturnRate": 71.9,
        "topChatters": [
            {
                "login": "viewer_alpha",
                "totalMessages": 1840,
                "totalSessions": 13,
                "firstSeen": _date(90),
                "lastSeen": _date(2),
                "loyaltyScore": 94,
            },
            {
                "login": "deadlock_fan1",
                "totalMessages": 1290,
                "totalSessions": 11,
                "firstSeen": _date(85),
                "lastSeen": _date(4),
                "loyaltyScore": 88,
            },
            {
                "login": "proplayer_de",
                "totalMessages": 1105,
                "totalSessions": 12,
                "firstSeen": _date(70),
                "lastSeen": _date(2),
                "loyaltyScore": 86,
            },
            {
                "login": "chat_lurker",
                "totalMessages": 980,
                "totalSessions": 9,
                "firstSeen": _date(60),
                "lastSeen": _date(5),
                "loyaltyScore": 81,
            },
            {
                "login": "kappa_guy_de",
                "totalMessages": 870,
                "totalSessions": 10,
                "firstSeen": _date(55),
                "lastSeen": _date(7),
                "loyaltyScore": 79,
            },
            {
                "login": "gamerfan99",
                "totalMessages": 740,
                "totalSessions": 8,
                "firstSeen": _date(45),
                "lastSeen": _date(9),
                "loyaltyScore": 75,
            },
            {
                "login": "nachteule_gg",
                "totalMessages": 695,
                "totalSessions": 7,
                "firstSeen": _date(40),
                "lastSeen": _date(11),
                "loyaltyScore": 72,
            },
            {
                "login": "rankgrinder",
                "totalMessages": 612,
                "totalSessions": 8,
                "firstSeen": _date(35),
                "lastSeen": _date(4),
                "loyaltyScore": 70,
            },
            {
                "login": "twitch_regular",
                "totalMessages": 580,
                "totalSessions": 6,
                "firstSeen": _date(30),
                "lastSeen": _date(6),
                "loyaltyScore": 67,
            },
            {
                "login": "viewer_beta",
                "totalMessages": 520,
                "totalSessions": 5,
                "firstSeen": _date(25),
                "lastSeen": _date(8),
                "loyaltyScore": 63,
            },
        ],
    }


def get_viewer_overlap() -> List[Dict[str, Any]]:
    peers = [
        ("deadlock_de_1", 142, 340, 0.418),
        ("deadlock_de_2", 118, 280, 0.421),
        ("deadlock_de_3", 95, 260, 0.365),
        ("fps_master_de", 87, 390, 0.223),
        ("ranked_grinder", 74, 310, 0.239),
        ("pro_viewer_de", 68, 420, 0.162),
        ("casual_gamer", 55, 210, 0.262),
        ("deadlock_fan", 49, 180, 0.272),
    ]
    return [
        {
            "streamerA": DEMO_STREAMER,
            "streamerB": b,
            "sharedChatters": sc,
            "totalChattersA": 634,
            "totalChattersB": tc,
            "overlapPercentage": round(op * 100, 1),
        }
        for b, sc, tc, op in peers
    ]


def get_tag_analysis() -> List[Dict[str, Any]]:
    return [
        {
            "tagName": "Deadlock",
            "usageCount": 14,
            "avgViewers": 382,
            "avgRetention10m": 71.2,
            "avgFollowerGain": 13.1,
        },
        {
            "tagName": "Deutsch",
            "usageCount": 14,
            "avgViewers": 382,
            "avgRetention10m": 71.2,
            "avgFollowerGain": 13.1,
        },
        {
            "tagName": "FPS",
            "usageCount": 12,
            "avgViewers": 375,
            "avgRetention10m": 70.8,
            "avgFollowerGain": 12.4,
        },
        {
            "tagName": "PC",
            "usageCount": 14,
            "avgViewers": 382,
            "avgRetention10m": 71.2,
            "avgFollowerGain": 13.1,
        },
        {
            "tagName": "Ranked",
            "usageCount": 10,
            "avgViewers": 398,
            "avgRetention10m": 73.4,
            "avgFollowerGain": 14.2,
        },
        {
            "tagName": "Controller",
            "usageCount": 4,
            "avgViewers": 355,
            "avgRetention10m": 68.1,
            "avgFollowerGain": 10.8,
        },
        {
            "tagName": "Shooter",
            "usageCount": 8,
            "avgViewers": 371,
            "avgRetention10m": 70.1,
            "avgFollowerGain": 12.0,
        },
    ]


def _tag_analysis_extended() -> List[Dict[str, Any]]:
    base = get_tag_analysis()
    extras = [
        ("up", 8.4, "19:00-22:00", 3.5, 4),
        ("up", 5.1, "18:00-21:00", 3.4, 4),
        ("stable", 1.2, "19:00-22:00", 3.4, 6),
        ("up", 3.3, "18:00-22:00", 3.5, 3),
        ("up", 12.7, "19:00-22:00", 3.6, 2),
        ("down", -4.2, "20:00-23:00", 3.2, 12),
        ("stable", 0.8, "19:00-22:00", 3.3, 5),
    ]
    result = []
    for tag, (trend, tv, slot, dur, rank) in zip(base, extras):
        result.append(
            {
                **tag,
                "trend": trend,
                "trendValue": tv,
                "bestTimeSlot": slot,
                "avgStreamDuration": dur,
                "categoryRank": rank,
            }
        )
    return result


def get_tag_analysis_extended() -> List[Dict[str, Any]]:
    return _tag_analysis_extended()


def _title_performance() -> List[Dict[str, Any]]:
    return [
        {
            "title": "Deadlock Ranked Grind | Platin → Diamond",
            "usageCount": 6,
            "avgViewers": 398,
            "avgRetention10m": 73.1,
            "avgFollowerGain": 14.2,
            "peakViewers": 1087,
            "keywords": ["Ranked", "Platin", "Diamond", "Grind"],
        },
        {
            "title": "Neue Patch-Analyse + Ranked | !guide",
            "usageCount": 2,
            "avgViewers": 412,
            "avgRetention10m": 74.8,
            "avgFollowerGain": 15.6,
            "peakViewers": 1040,
            "keywords": ["Patch", "Analyse", "Guide", "Ranked"],
        },
        {
            "title": "Ranked mit Freunden | !discord",
            "usageCount": 2,
            "avgViewers": 377,
            "avgRetention10m": 69.8,
            "avgFollowerGain": 12.1,
            "peakViewers": 940,
            "keywords": ["Ranked", "Discord", "Freunde"],
        },
        {
            "title": "Ranked mit Zuschauern | Komm rein!",
            "usageCount": 2,
            "avgViewers": 390,
            "avgRetention10m": 72.3,
            "avgFollowerGain": 13.4,
            "peakViewers": 970,
            "keywords": ["Ranked", "Zuschauer"],
        },
        {
            "title": "Deadlock Solo Q | Road to Diamond",
            "usageCount": 3,
            "avgViewers": 368,
            "avgRetention10m": 68.9,
            "avgFollowerGain": 11.8,
            "peakViewers": 890,
            "keywords": ["Solo", "Diamond", "Road"],
        },
    ]


def get_title_performance() -> List[Dict[str, Any]]:
    return _title_performance()


def get_rankings(metric: str = "viewers") -> List[Dict[str, Any]]:
    data = {
        "viewers": [
            (1, "deadlock_de_top1", 892, "up", 12.4),
            (2, "fps_king_de", 721, "up", 4.1),
            (3, "ranked_master_de", 612, "same", 0.0),
            (4, "deadlock_de_2", 521, "down", -3.2),
            (5, "casual_fps_de", 487, "up", 6.8),
            (6, "fps_nerd_de", 445, "up", 2.1),
            (7, "deadlock_de_3", 430, "down", -1.4),
            (8, "pro_gamer_de", 408, "same", 0.0),
            (9, DEMO_STREAMER, 382, "up", 6.8),
            (10, "new_streamer_de", 371, "up", 18.3),
        ],
        "growth": [
            (1, "new_streamer_de", 42.1, "up", 8.3),
            (2, "deadlock_de_top1", 18.7, "up", 2.1),
            (3, DEMO_STREAMER, 12.4, "up", 1.2),
            (4, "fps_king_de", 10.8, "up", 0.4),
            (5, "ranked_master_de", 8.2, "same", 0.0),
            (6, "casual_fps_de", 7.4, "up", 1.1),
            (7, "fps_nerd_de", 5.1, "down", -0.3),
            (8, "deadlock_de_2", 3.8, "down", -1.2),
            (9, "deadlock_de_3", 2.1, "same", 0.0),
            (10, "pro_gamer_de", -0.4, "down", -2.1),
        ],
        "retention": [
            (1, "ranked_master_de", 78.4, "up", 1.2),
            (2, "deadlock_de_top1", 76.1, "up", 0.8),
            (3, "fps_king_de", 74.9, "same", 0.0),
            (4, DEMO_STREAMER, 71.2, "up", 1.9),
            (5, "casual_fps_de", 69.4, "up", 0.4),
            (6, "fps_nerd_de", 68.8, "down", -0.3),
            (7, "pro_gamer_de", 67.2, "same", 0.0),
            (8, "deadlock_de_2", 65.9, "down", -1.1),
            (9, "deadlock_de_3", 64.1, "up", 0.2),
            (10, "new_streamer_de", 61.8, "up", 3.4),
        ],
        "chat": [
            (1, "deadlock_de_top1", 21.4, "up", 2.8),
            (2, "fps_king_de", 18.9, "up", 1.1),
            (3, "casual_fps_de", 17.2, "same", 0.0),
            (4, DEMO_STREAMER, 14.2, "up", 1.4),
            (5, "ranked_master_de", 13.8, "down", -0.2),
            (6, "fps_nerd_de", 12.4, "up", 0.8),
            (7, "deadlock_de_2", 11.9, "same", 0.0),
            (8, "pro_gamer_de", 11.2, "down", -0.4),
            (9, "deadlock_de_3", 10.8, "up", 0.1),
            (10, "new_streamer_de", 9.4, "up", 2.2),
        ],
    }
    rows = data.get(metric, data["viewers"])
    return [
        {"rank": rank, "login": login, "value": value, "trend": trend, "trendValue": tv}
        for rank, login, value, trend, tv in rows
    ]


def get_category_comparison() -> Dict[str, Any]:
    return {
        "yourStats": {
            "avgViewers": 382,
            "peakViewers": 1087,
            "retention10m": 71.2,
            "chatHealth": 74,
        },
        "categoryAvg": {
            "avgViewers": 284,
            "peakViewers": 780,
            "retention10m": 63.4,
            "chatHealth": 61,
        },
        "percentiles": {
            "avgViewers": 79,
            "peakViewers": 82,
            "retention10m": 74,
            "chatHealth": 77,
        },
        "categoryRank": 12,
        "categoryTotal": 58,
    }


def _watch_time_distribution() -> Dict[str, Any]:
    return {
        "under5min": 12.4,
        "min5to15": 18.7,
        "min15to30": 21.3,
        "min30to60": 26.8,
        "over60min": 20.8,
        "avgWatchTime": 38.4,
        "medianWatchTime": 29.1,
        "sessionCount": 14,
        "previous": {
            "under5min": 14.1,
            "min5to15": 19.8,
            "min15to30": 22.1,
            "min30to60": 25.2,
            "over60min": 18.8,
            "avgWatchTime": 35.2,
            "medianWatchTime": 26.8,
            "sessionCount": 13,
        },
        "deltas": {
            "under5min": -1.7,
            "min5to15": -1.1,
            "min15to30": -0.8,
            "min30to60": 1.6,
            "over60min": 2.0,
            "avgWatchTime": 3.2,
        },
    }


def get_watch_time_distribution() -> Dict[str, Any]:
    return _watch_time_distribution()


def _follower_funnel() -> Dict[str, Any]:
    return {
        "uniqueViewers": 1840,
        "returningViewers": 680,
        "newFollowers": 183,
        "netFollowerDelta": 167,
        "conversionRate": 9.95,
        "avgTimeToFollow": 18.4,
        "followersBySource": {"organic": 124, "raids": 41, "hosts": 8, "other": 10},
    }


def get_follower_funnel() -> Dict[str, Any]:
    return _follower_funnel()


def get_audience_insights() -> Dict[str, Any]:
    return {
        "watchTimeDistribution": _watch_time_distribution(),
        "followerFunnel": _follower_funnel(),
        "tagPerformance": _tag_analysis_extended(),
        "titlePerformance": _title_performance(),
        "trends": {
            "watchTimeChange": 8.4,
            "conversionChange": 2.1,
            "viewerReturnRate": 38.2,
            "viewerReturnChange": 4.7,
        },
    }


def get_audience_demographics() -> Dict[str, Any]:
    return {
        "estimatedRegions": [
            {"region": "DE", "percentage": 58.4},
            {"region": "AT", "percentage": 12.1},
            {"region": "CH", "percentage": 8.7},
            {"region": "EU (andere)", "percentage": 14.2},
            {"region": "Sonstige", "percentage": 6.6},
        ],
        "viewerTypes": [
            {"label": "Loyale Stamm-Viewer", "percentage": 38.2},
            {"label": "Wiederkehrende Viewer", "percentage": 29.4},
            {"label": "Neue Viewer", "percentage": 21.8},
            {"label": "Zufällige Besucher", "percentage": 10.6},
        ],
        "activityPattern": "weekend-heavy",
        "primaryLanguage": "de",
        "languageConfidence": 0.92,
        "peakActivityHours": [19, 20, 21, 22],
        "interactiveRate": 0.134,
        "loyaltyScore": 72,
    }


def get_viewer_timeline(days: int = 30) -> List[Dict[str, Any]]:
    result = []
    today = datetime.now(timezone.utc)
    for i in range(min(days, 30), 0, -1):
        dt = today - timedelta(days=i)
        wd = dt.weekday()
        if wd in (0, 2, 4, 5):
            avg = 382 + (hash(str(dt.date())) % 60) - 30
            peak = avg + 180 + (hash(str(dt.date()) + "p") % 100)
            mn = max(50, avg - 120)
        else:
            avg = 0
            peak = 0
            mn = 0
        result.append(
            {
                "timestamp": dt.replace(
                    hour=20, minute=0, second=0, microsecond=0
                ).isoformat(),
                "avgViewers": avg,
                "peakViewers": peak,
                "minViewers": mn,
                "samples": 4 if avg > 0 else 0,
            }
        )
    return result


def get_category_leaderboard() -> Dict[str, Any]:
    entries = [
        (1, "deadlock_de_top1", 892, 2140, True),
        (2, "fps_king_de", 721, 1820, True),
        (3, "ranked_master_de", 612, 1540, True),
        (4, "deadlock_de_2", 521, 1290, True),
        (5, "casual_fps_de", 487, 1180, False),
        (6, "fps_nerd_de", 445, 1090, True),
        (7, "deadlock_de_3", 430, 1060, False),
        (8, "pro_gamer_de", 408, 1010, True),
        (9, DEMO_STREAMER, 382, 940, True),
        (10, "new_streamer_de", 371, 910, False),
        (11, "fr_player", 348, 870, False),
        (12, "en_streamer", 332, 840, False),
    ]
    return {
        "leaderboard": [
            {
                "rank": r,
                "streamer": s,
                "avgViewers": av,
                "peakViewers": pv,
                "isPartner": ip,
                "isYou": s == DEMO_STREAMER,
            }
            for r, s, av, pv, ip in entries
        ],
        "totalStreamers": 58,
        "yourRank": 9,
    }


def get_monetization() -> Dict[str, Any]:
    return {
        "ads": {
            "total": 38,
            "auto": 22,
            "manual": 16,
            "sessions_with_ads": 11,
            "avg_duration_s": 31.4,
            "avg_viewer_drop_pct": 8.2,
            "worst_ads": [
                {
                    "started_at": _dt(5, 20),
                    "duration_s": 90,
                    "drop_pct": 18.4,
                    "is_automatic": True,
                },
                {
                    "started_at": _dt(12, 21),
                    "duration_s": 60,
                    "drop_pct": 14.1,
                    "is_automatic": False,
                },
                {
                    "started_at": _dt(19, 20),
                    "duration_s": 60,
                    "drop_pct": 12.8,
                    "is_automatic": True,
                },
            ],
        },
        "hype_train": {
            "total": 4,
            "avg_level": 2.8,
            "max_level": 4,
            "avg_duration_s": 312,
        },
        "bits": {"total": 28400, "cheer_events": 47},
        "subs": {"total_events": 312, "gifted": 84},
        "window_days": 30,
    }


def get_category_timings() -> Dict[str, Any]:
    hourly = []
    for h in range(24):
        if 17 <= h <= 23:
            median = 380 + (h - 17) * 20
            p25 = median - 80
            p75 = median + 120
            sc = 12
            samp = 48
        elif 12 <= h <= 16:
            median = 220
            p25 = 160
            p75 = 290
            sc = 6
            samp = 18
        else:
            median = None
            p25 = None
            p75 = None
            sc = 0
            samp = 0
        hourly.append(
            {
                "hour": h,
                "median": median,
                "p25": p25,
                "p75": p75,
                "streamer_count": sc,
                "sample_count": samp,
            }
        )
    weekly_data = [
        (0, "Montag", 410, 320, 530),
        (1, "Dienstag", 380, 300, 490),
        (2, "Mittwoch", 420, 340, 560),
        (3, "Donnerstag", 360, 280, 470),
        (4, "Freitag", 480, 390, 640),
        (5, "Samstag", 520, 420, 680),
        (6, "Sonntag", 390, 310, 510),
    ]
    weekly = [
        {
            "weekday": wd,
            "label": lbl,
            "median": med,
            "p25": p25,
            "p75": p75,
            "streamer_count": 24,
            "sample_count": 96,
        }
        for wd, lbl, med, p25, p75 in weekly_data
    ]
    return {
        "hourly": hourly,
        "weekly": weekly,
        "total_streamers": 58,
        "window_days": 30,
        "method": "median",
    }


def get_category_activity_series() -> Dict[str, Any]:
    hourly = []
    for h in range(24):
        if 17 <= h <= 23:
            cat_avg = 320 + (h - 17) * 18
            tr_avg = 382 + (h - 17) * 12
            cat_peak = cat_avg + 240
            tr_peak = tr_avg + 200
            c_samp = 380
            t_samp = 48
        elif 12 <= h <= 16:
            cat_avg = 180
            tr_avg = 0
            cat_peak = 280
            tr_peak = 0
            c_samp = 120
            t_samp = 0
        else:
            cat_avg = 0
            tr_avg = 0
            cat_peak = 0
            tr_peak = 0
            c_samp = 0
            t_samp = 0
        hourly.append(
            {
                "hour": h,
                "label": f"{h:02d}:00",
                "categoryAvg": cat_avg or None,
                "trackedAvg": tr_avg or None,
                "categoryPeak": cat_peak or None,
                "trackedPeak": tr_peak or None,
                "categorySamples": c_samp,
                "trackedSamples": t_samp,
            }
        )
    weekly_data = [
        (0, "Mo", 380, 372, 890, 860),
        (1, "Di", 340, None, 780, None),
        (2, "Mi", 390, 388, 920, 940),
        (3, "Do", 320, 345, 740, 780),
        (4, "Fr", 450, 401, 1040, 1020),
        (5, "Sa", 480, 423, 1120, 1087),
        (6, "So", 360, 310, 840, 650),
    ]
    weekly = [
        {
            "weekday": wd,
            "label": lbl,
            "categoryAvg": ca,
            "trackedAvg": ta,
            "categoryPeak": cp,
            "trackedPeak": tp,
            "categorySamples": 200,
            "trackedSamples": 14 if ta else 0,
        }
        for wd, lbl, ca, ta, cp, tp in weekly_data
    ]
    return {"hourly": hourly, "weekly": weekly, "windowDays": 30, "source": "mixed"}


def get_coaching() -> Dict[str, Any]:
    return {
        "streamer": DEMO_STREAMER,
        "days": 30,
        "empty": False,
        "efficiency": {
            "viewerHoursPerStreamHour": 27.4,
            "categoryAvg": 21.8,
            "topPerformers": [
                {"streamer": "deadlock_de_top1", "ratio": 42.1},
                {"streamer": "fps_king_de", "ratio": 36.8},
                {"streamer": DEMO_STREAMER, "ratio": 27.4},
            ],
            "percentile": 74,
            "totalStreamHours": 194.8,
            "totalViewerHours": 5342,
        },
        "titleAnalysis": {
            "yourTitles": [
                {
                    "title": "Deadlock Ranked Grind | Platin → Diamond",
                    "avgViewers": 398,
                    "peakViewers": 1087,
                    "chatters": 52,
                    "usageCount": 6,
                },
                {
                    "title": "Neue Patch-Analyse + Ranked | !guide",
                    "avgViewers": 412,
                    "peakViewers": 1040,
                    "chatters": 55,
                    "usageCount": 2,
                },
                {
                    "title": "Ranked mit Zuschauern | Komm rein!",
                    "avgViewers": 390,
                    "peakViewers": 970,
                    "chatters": 50,
                    "usageCount": 2,
                },
            ],
            "categoryTopTitles": [
                {
                    "title": "Deadlock HIGH ELO | !discord",
                    "streamer": "deadlock_de_top1",
                    "avgViewers": 892,
                },
                {
                    "title": "Ranked Push | Platin → Diamond",
                    "streamer": "fps_king_de",
                    "avgViewers": 721,
                },
            ],
            "yourMissingPatterns": ["!discord im Titel", "Rank-Fortschritt sichtbar"],
            "topPerformerPatterns": [
                "!discord im Titel",
                "HIGH ELO",
                "Rank-Ziel explizit",
            ],
            "varietyPct": 35.7,
            "uniqueTitleCount": 5,
            "totalSessionCount": 14,
            "avgPeerVarietyPct": 42.1,
            "peerVariety": [
                {
                    "streamer": "deadlock_de_top1",
                    "uniqueTitles": 9,
                    "totalSessions": 16,
                    "varietyPct": 56.3,
                },
                {
                    "streamer": DEMO_STREAMER,
                    "uniqueTitles": 5,
                    "totalSessions": 14,
                    "varietyPct": 35.7,
                },
            ],
        },
        "scheduleOptimizer": {
            "sweetSpots": [
                {
                    "weekday": 5,
                    "hour": 17,
                    "categoryViewers": 1840,
                    "competitors": 4,
                    "opportunityScore": 88,
                },
                {
                    "weekday": 6,
                    "hour": 17,
                    "categoryViewers": 1920,
                    "competitors": 5,
                    "opportunityScore": 84,
                },
                {
                    "weekday": 3,
                    "hour": 19,
                    "categoryViewers": 1480,
                    "competitors": 3,
                    "opportunityScore": 78,
                },
            ],
            "yourCurrentSlots": [
                {"weekday": 0, "hour": 19, "count": 3},
                {"weekday": 2, "hour": 19, "count": 4},
                {"weekday": 4, "hour": 20, "count": 3},
                {"weekday": 5, "hour": 20, "count": 2},
            ],
            "competitionHeatmap": [],
        },
        "durationAnalysis": {
            "buckets": [
                {
                    "label": "< 2h",
                    "streamCount": 1,
                    "avgViewers": 310,
                    "avgChatters": 32,
                    "avgRetention5m": 78.1,
                    "efficiencyRatio": 18.4,
                },
                {
                    "label": "2-3h",
                    "streamCount": 3,
                    "avgViewers": 362,
                    "avgChatters": 44,
                    "avgRetention5m": 82.4,
                    "efficiencyRatio": 24.8,
                },
                {
                    "label": "3-4h",
                    "streamCount": 7,
                    "avgViewers": 388,
                    "avgChatters": 49,
                    "avgRetention5m": 84.2,
                    "efficiencyRatio": 27.9,
                },
                {
                    "label": "4-5h",
                    "streamCount": 3,
                    "avgViewers": 395,
                    "avgChatters": 51,
                    "avgRetention5m": 83.8,
                    "efficiencyRatio": 28.4,
                },
                {
                    "label": "> 5h",
                    "streamCount": 0,
                    "avgViewers": 0,
                    "avgChatters": 0,
                    "avgRetention5m": 0.0,
                    "efficiencyRatio": 0.0,
                },
            ],
            "optimalLabel": "3-4h",
            "currentAvgHours": 3.4,
            "correlation": 0.42,
        },
        "crossCommunity": {
            "totalUniqueChatters": 634,
            "chatterSources": [
                {
                    "sourceStreamer": "deadlock_de_top1",
                    "sharedChatters": 142,
                    "percentage": 22.4,
                },
                {
                    "sourceStreamer": "deadlock_de_2",
                    "sharedChatters": 118,
                    "percentage": 18.6,
                },
                {
                    "sourceStreamer": "fps_king_de",
                    "sharedChatters": 95,
                    "percentage": 15.0,
                },
            ],
            "isolatedChatters": 187,
            "isolatedPercentage": 29.5,
            "ecosystemSummary": "Gut vernetzt im deutschen Deadlock-Ökosystem. ~70 % deiner Chatter schauen auch andere Partner.",
        },
        "tagOptimization": {
            "yourTags": [
                {
                    "tags": "Deadlock, Deutsch, Ranked",
                    "avgViewers": 398,
                    "usageCount": 10,
                },
                {"tags": "Deadlock, Deutsch, FPS", "avgViewers": 375, "usageCount": 4},
            ],
            "categoryBestTags": [
                {
                    "tags": "Deadlock, Deutsch, HIGH ELO",
                    "avgViewers": 620,
                    "streamerCount": 3,
                },
                {
                    "tags": "Deadlock, Ranked, !discord",
                    "avgViewers": 540,
                    "streamerCount": 5,
                },
            ],
            "missingHighPerformers": ["HIGH ELO", "!discord im Tag"],
            "underperformingTags": ["Controller"],
        },
        "retentionCoaching": {
            "your5mRetention": 83.7,
            "category5mRetention": 76.2,
            "yourViewerCurve": [
                {"minute": m, "avgViewerPct": max(40, 100 - m * 2.1 - (m > 5) * 3)}
                for m in range(0, 31, 5)
            ],
            "topPerformerCurve": [
                {"minute": m, "avgViewerPct": max(50, 100 - m * 1.6 - (m > 5) * 2)}
                for m in range(0, 31, 5)
            ],
            "criticalDropoffMinute": 4,
        },
        "doubleStreamDetection": {
            "detected": False,
            "count": 0,
            "occurrences": [],
            "singleDayAvg": 382,
            "doubleDayAvg": 0,
        },
        "chatConcentration": {
            "totalChatters": 634,
            "totalMessages": 84320,
            "msgsPerChatter": 133,
            "loyaltyBuckets": {
                "Einmalig (1-2)": {"count": 178, "pct": 28.1, "messages": 267},
                "Gelegentlich (3-9)": {"count": 212, "pct": 33.4, "messages": 8480},
                "Regelmäßig (10-29)": {"count": 148, "pct": 23.3, "messages": 20720},
                "Stamm-Viewer (30+)": {"count": 96, "pct": 15.1, "messages": 54853},
            },
            "topChatters": [
                {
                    "login": "viewer_alpha",
                    "messages": 1840,
                    "sessions": 13,
                    "sharePct": 2.18,
                    "cumulativePct": 2.18,
                },
                {
                    "login": "deadlock_fan1",
                    "messages": 1290,
                    "sessions": 11,
                    "sharePct": 1.53,
                    "cumulativePct": 3.71,
                },
            ],
            "concentrationIndex": 0.48,
            "top1Pct": 6.8,
            "top3Pct": 14.2,
            "ownOneTimerPct": 28.1,
            "avgPeerOneTimerPct": 32.4,
        },
        "raidNetwork": {
            "totalSent": 8,
            "totalReceived": 5,
            "totalSentViewers": 2840,
            "totalReceivedViewers": 1620,
            "avgSentViewers": 355,
            "avgReceivedViewers": 324,
            "reciprocityRatio": 0.625,
            "mutualPartners": 3,
            "totalPartners": 6,
            "partners": [
                {
                    "login": "deadlock_de_1",
                    "sentCount": 3,
                    "sentAvgViewers": 380,
                    "receivedCount": 2,
                    "receivedAvgViewers": 340,
                    "reciprocity": "mutual",
                    "balance": 1,
                },
                {
                    "login": "fps_king_de",
                    "sentCount": 2,
                    "sentAvgViewers": 350,
                    "receivedCount": 2,
                    "receivedAvgViewers": 310,
                    "reciprocity": "mutual",
                    "balance": 0,
                },
                {
                    "login": "deadlock_de_3",
                    "sentCount": 2,
                    "sentAvgViewers": 340,
                    "receivedCount": 0,
                    "receivedAvgViewers": 0,
                    "reciprocity": "sentOnly",
                    "balance": 2,
                },
                {
                    "login": "casual_fps_de",
                    "sentCount": 1,
                    "sentAvgViewers": 360,
                    "receivedCount": 1,
                    "receivedAvgViewers": 290,
                    "reciprocity": "mutual",
                    "balance": 0,
                },
                {
                    "login": "fps_nerd_de",
                    "sentCount": 0,
                    "sentAvgViewers": 0,
                    "receivedCount": 2,
                    "receivedAvgViewers": 310,
                    "reciprocity": "receivedOnly",
                    "balance": -2,
                },
            ],
        },
        "peerComparison": {
            "ownData": {
                "login": DEMO_STREAMER,
                "sessions": 14,
                "avgViewers": 382,
                "maxPeak": 1087,
                "avgHours": 3.4,
                "avgChatters": 45,
                "retention5m": 83.7,
                "totalHours": 47.6,
                "followsGained": 183,
                "uniqueTitles": 5,
                "titleVariety": 35.7,
            },
            "ownRank": 9,
            "totalStreamers": 58,
            "similarPeers": [
                {
                    "login": "fps_nerd_de",
                    "sessions": 13,
                    "avgViewers": 445,
                    "maxPeak": 1090,
                    "avgHours": 3.2,
                    "avgChatters": 48,
                    "retention5m": 84.1,
                    "totalHours": 41.6,
                    "followsGained": 145,
                    "uniqueTitles": 6,
                    "titleVariety": 46.2,
                },
                {
                    "login": "deadlock_de_3",
                    "sessions": 15,
                    "avgViewers": 430,
                    "maxPeak": 1060,
                    "avgHours": 3.5,
                    "avgChatters": 46,
                    "retention5m": 82.9,
                    "totalHours": 52.5,
                    "followsGained": 138,
                    "uniqueTitles": 5,
                    "titleVariety": 33.3,
                },
            ],
            "aspirationalPeers": [
                {
                    "login": "deadlock_de_top1",
                    "sessions": 16,
                    "avgViewers": 892,
                    "maxPeak": 2140,
                    "avgHours": 4.1,
                    "avgChatters": 98,
                    "retention5m": 88.4,
                    "totalHours": 65.6,
                    "followsGained": 412,
                    "uniqueTitles": 9,
                    "titleVariety": 56.3,
                },
            ],
            "metricsRanked": {
                "avgViewers": {"rank": 9, "total": 58, "value": 382},
                "retention5m": {"rank": 6, "total": 58, "value": 83.7},
                "growth": {"rank": 3, "total": 58, "value": 12.4},
                "chatHealth": {"rank": 10, "total": 58, "value": 74},
            },
            "gapToNext": {
                "login": "pro_gamer_de",
                "avgViewersDiff": 26,
                "chatDiff": 3,
                "retentionDiff": 0.6,
            },
        },
        "competitionDensity": {
            "hourly": [
                {
                    "hour": h,
                    "activeStreamers": max(0, 18 - abs(h - 20) * 2),
                    "avgViewers": max(0, 380 - abs(h - 20) * 40),
                    "avgPeak": max(0, 920 - abs(h - 20) * 80),
                    "opportunityScore": max(0, 60 - abs(h - 17) * 8),
                    "yourData": {
                        "count": 1,
                        "avgViewers": 382,
                        "avgPeak": 940,
                        "avgChatters": 45,
                    }
                    if h in (19, 20)
                    else None,
                }
                for h in range(24)
            ],
            "weekly": [
                {
                    "weekday": wd,
                    "weekdayLabel": lbl,
                    "activeStreamers": sc,
                    "avgViewers": av,
                    "yourData": {"count": yc, "avgViewers": ya, "avgPeak": yp}
                    if yc
                    else None,
                }
                for wd, lbl, sc, av, yc, ya, yp in [
                    (0, "Montag", 14, 310, 3, 372, 890),
                    (1, "Dienstag", 12, 280, 0, 0, 0),
                    (2, "Mittwoch", 16, 330, 4, 388, 940),
                    (3, "Donnerstag", 11, 260, 1, 345, 780),
                    (4, "Freitag", 18, 380, 3, 401, 1020),
                    (5, "Samstag", 20, 420, 2, 423, 1087),
                    (6, "Sonntag", 10, 280, 2, 310, 650),
                ]
            ],
            "sweetSpots": [
                {
                    "hour": 17,
                    "activeStreamers": 8,
                    "avgViewers": 1840,
                    "avgPeak": 2100,
                    "opportunityScore": 88,
                    "yourData": None,
                },
                {
                    "hour": 18,
                    "activeStreamers": 10,
                    "avgViewers": 1920,
                    "avgPeak": 2240,
                    "opportunityScore": 82,
                    "yourData": None,
                },
            ],
        },
        "recommendations": [
            {
                "priority": "high",
                "category": "Schedule",
                "icon": "⏰",
                "title": "Früher starten: Samstag 17:00 testen",
                "description": "Samstag 17–19 Uhr hat die niedrigste Konkurrenz-Dichte aber hohe Viewer-Nachfrage in der Kategorie.",
                "estimatedImpact": "+15–25 % Avg Viewer",
                "evidence": "12 Streams in diesem Slot haben 21 % mehr Avg Viewer als deine aktuellen Samstag-Starts.",
            },
            {
                "priority": "medium",
                "category": "Titel",
                "icon": "✏️",
                "title": "!discord in Titel aufnehmen",
                "description": "Top-Performer nutzen !discord-Referenz im Titel. Zeigt Community-Stärke und konvertiert besser.",
                "estimatedImpact": "+8–12 % Follower-Conversion",
                "evidence": "Streamer mit !discord im Titel haben 18 % höhere Conversion-Rate.",
            },
            {
                "priority": "medium",
                "category": "Retention",
                "icon": "📈",
                "title": "Erste 5 Minuten straffen",
                "description": "Kritischer Dropoff in Minute 3–5. Kürze Intro, starte früher mit Gameplay.",
                "estimatedImpact": "+3–5 % Retention",
                "evidence": "Deine 5m-Retention ist gut (83 %), aber Minute 3–5 zeigt messbaren Einbruch.",
            },
            {
                "priority": "low",
                "category": "Titel",
                "icon": "🎯",
                "title": "Mehr Titel-Variation einsetzen",
                "description": "35 % Titel-Variety vs. 42 % Kategorie-Durchschnitt. Abwechslung signalisiert frischen Content.",
                "estimatedImpact": "+5 % Neue Viewer",
                "evidence": "Peers mit 50 %+ Variety gewinnen 22 % mehr Erst-Viewer pro Stream.",
            },
        ],
        "aiSummary": (
            "**Deadlock_DE_Demo** ist ein gut etablierter Mid-Tier-Streamer im deutschen Deadlock-Ökosystem. "
            "Deine Retention (71 % bei 10 min) liegt 12 % über dem Kategorie-Durchschnitt – ein starkes Fundament. "
            "Das größte Wachstumspotenzial liegt im **Schedule-Timing**: Samstag 17:00 Uhr hat aktuell wenig Konkurrenz, "
            "aber hohe Viewer-Nachfrage. Dein Raid-Netzwerk ist aktiv und Community-gesund. "
            "Kurzfristig: **!discord in Titel aufnehmen** und **frühere Samstag-Starts testen**."
        ),
    }
