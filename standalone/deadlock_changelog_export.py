"""Utilities to export Deadlock changelog forum threads to CSV."""

from __future__ import annotations

import csv
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://forums.playdeadlock.com"
CHANGELOG_LIST_URL = urljoin(BASE_URL, "/forums/changelog.10/")


@dataclass
class ThreadMetadata:
    """Simple container for exported changelog thread information."""

    title: str
    url: str
    posted_at: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "posted_at": self.posted_at,
            "content": self.content,
        }


def fetch_soup(url: str) -> BeautifulSoup:
    """Fetch the given URL and return a BeautifulSoup document."""

    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def iter_changelog_threads() -> Generator[str, None, None]:
    """Yield all changelog thread URLs by walking the pagination."""

    next_url = CHANGELOG_LIST_URL

    while next_url:
        soup = fetch_soup(next_url)
        thread_container = soup.select("div.structItemContainer-group.js-threadList")
        for container in thread_container:
            for link in container.select(
                "div.structItem-cell.structItem-cell--main div.structItem-title a"
            ):
                href = link.get("href")
                if not href:
                    continue
                yield urljoin(BASE_URL, href)

        next_link = soup.select_one("a.pageNav-jump--next")
        next_url = urljoin(BASE_URL, next_link["href"]) if next_link else None


def fetch_thread_content(thread_url: str) -> ThreadMetadata:
    """Fetch a single thread and extract the relevant metadata."""

    soup = fetch_soup(thread_url)

    title_element = soup.select_one("h1.p-title-value")
    title = title_element.get_text(strip=True) if title_element else ""

    time_element = soup.select_one("time[data-timestamp]")
    if time_element:
        posted_at = (
            time_element.get("datetime")
            or time_element.get("data-datestring")
            or time_element.get("data-time")
            or time_element.get("data-timestamp")
            or ""
        )
    else:
        posted_at = ""

    content_element = soup.select_one("div.bbWrapper")
    if content_element:
        content = " ".join(content_element.stripped_strings)
    else:
        content = ""

    return ThreadMetadata(title=title, url=thread_url, posted_at=posted_at, content=content)


def export_threads(threads: Iterable[ThreadMetadata], output_path: Path) -> int:
    """Export the provided thread metadata to the given CSV path."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [thread.as_dict() for thread in threads]

    with output_path.open("w", encoding="utf-8", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["title", "url", "posted_at", "content"])
        writer.writeheader()
        writer.writerows(rows)

    return len(rows)


def main() -> None:
    threads = [fetch_thread_content(thread_url) for thread_url in iter_changelog_threads()]
    output_path = Path(__file__).with_name("deadlock_changelogs.csv")
    count = export_threads(threads, output_path)
    print(f"Exported {count} changelog threads to {output_path.name}.")


if __name__ == "__main__":
    main()
