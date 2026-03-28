from cogs.bug_reporter import BugReporter


def test_infer_category_prioritizes_scam_reports_over_steam_keywords() -> None:
    reporter = object.__new__(BugReporter)

    category = reporter._infer_category(
        "Wurde jetzt das 3. Mal von einem Scammer angeschrieben wegen Steam Item Kauf Scam."
    )

    assert category == "user_management"


def test_infer_category_still_returns_steam_verification_for_linking_issues() -> None:
    reporter = object.__new__(BugReporter)

    category = reporter._infer_category(
        "Steam Verifizierung klappt nicht und der Verify Button reagiert nicht."
    )

    assert category == "steam_verification"
