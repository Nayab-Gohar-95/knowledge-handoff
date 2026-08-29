"""
contributor_agent.py — Knowledge Continuity Suite

Walk every tracked file in a target git repo, query git log per file to
extract author identities and timestamps, and emit a sorted JSON report to
outputs/contributor_report.json.

Usage:
    python agents/contributor_agent.py [repo_path]

repo_path defaults to the current working directory if omitted.
"""

import json
import os
import sys
from datetime import datetime, timezone

import git


def collect_tracked_files(tree) -> list[str]:
    """Recursively collect all blob paths from a git tree."""
    paths: list[str] = []
    for blob in tree.blobs:
        paths.append(blob.path)
    for subtree in tree.trees:
        paths.extend(collect_tracked_files(subtree))
    return paths


def analyze_file(repo: git.Repo, file_path: str, today: datetime.date) -> dict:
    """Return contributor metadata for a single tracked file."""
    raw_log = repo.git.log(
        "--follow",
        "--format=%aN%x09%ae%x09%aI",
        "--",
        file_path,
    )

    lines = [line for line in raw_log.splitlines() if line.strip()]

    # Deduplicate by author name only so the same person using two email
    # addresses counts as one contributor. Emails are collected per name
    # for reference in the output.
    author_emails: dict[str, set] = {}  # name -> set of emails seen
    last_touch_iso: str | None = None

    for line in lines:
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        name, email, timestamp_str = parts
        if name not in author_emails:
            author_emails[name] = set()
        author_emails[name].add(email)
        if last_touch_iso is None:
            # git log is newest-first; first line = most recent commit
            last_touch_iso = timestamp_str

    commit_count = len(lines)

    if last_touch_iso:
        last_touch_dt = datetime.fromisoformat(last_touch_iso)
        last_touch_date = last_touch_dt.date()
    else:
        last_touch_date = today

    days_since = (today - last_touch_date).days

    # Build author list as "Name <email1, email2>" for reference
    authors = [
        f"{name} <{', '.join(sorted(emails))}>"
        for name, emails in author_emails.items()
    ]

    return {
        "file": file_path,
        "author_count": len(author_emails),
        "authors": authors,
        "commit_count": commit_count,
        "last_touch_date": last_touch_date.isoformat(),
        "days_since_last_touch": days_since,
    }


def main() -> None:
    repo_path = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    repo_path = os.path.abspath(repo_path)

    repo = git.Repo(repo_path)
    today = datetime.now(timezone.utc).date()

    tracked_files = collect_tracked_files(repo.head.commit.tree)

    file_records = [analyze_file(repo, fp, today) for fp in tracked_files]

    # Sort: fewest authors first; ties broken by most days since last touch (desc)
    file_records.sort(key=lambda r: (r["author_count"], -r["days_since_last_touch"]))

    report = {
        "agent": "contributor_agent",
        "repo": repo_path,
        "file_count_analyzed": len(file_records),
        "files": file_records,
    }

    os.makedirs("outputs", exist_ok=True)
    output_path = os.path.join("outputs", "contributor_report.json")
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print(f"Analyzed {len(file_records)} files in {repo_path}")


if __name__ == "__main__":
    main()
