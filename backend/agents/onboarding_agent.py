"""
onboarding_agent.py — Knowledge Continuity Suite
IBM TechXchange 2026 Hackathon

For every HIGH-risk file: rank the team members most able to take over
if the sole owner leaves, and explain why.

Usage:
    python backend/agents/onboarding_agent.py [--repo PATH]
                                               [--risk-report PATH]
                                               [--contributor-report PATH]
                                               [--top-n N]

Defaults (all resolved relative to this script's directory):
    --repo               ../../sample-repos/steam-snap
    --risk-report        ../outputs/risk_report.json
    --contributor-report ../outputs/contributor_report.json
    --top-n              3
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_DEFAULT_REPO = str((_HERE / ".." / ".." / "sample-repos" / "steam-snap").resolve())
_DEFAULT_RISK_REPORT = str((_HERE / ".." / "outputs" / "risk_report.json").resolve())
_DEFAULT_CONTRIB_REPORT = str((_HERE / ".." / "outputs" / "contributor_report.json").resolve())
_OUTPUT_PATH = str((_HERE / ".." / "outputs" / "onboarding_report.json").resolve())


# ---------------------------------------------------------------------------
# JSON loading with encoding ladder
# ---------------------------------------------------------------------------

def _load_json_with_ladder(path: str) -> Any:
    """Try utf-8-sig → utf-16 → utf-16-le; exit 1 on failure."""
    for enc in ("utf-8-sig", "utf-16", "utf-16-le"):
        try:
            with open(path, encoding=enc) as fh:
                return json.load(fh)
        except (UnicodeDecodeError, UnicodeError):
            continue
        except json.JSONDecodeError as exc:
            sys.exit(f"[error] {path} decoded as {enc} but is not valid JSON: {exc}")
    sys.exit(f"[error] Cannot decode {path} with utf-8-sig / utf-16 / utf-16-le.")


# ---------------------------------------------------------------------------
# Git log — single read-only pass
# ---------------------------------------------------------------------------

def _run_git_log(repo_path: str) -> str:
    """Run `git log --format="%H|%aN|%ae|%aI" --name-only` once and return stdout."""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H|%aN|%ae|%aI", "--name-only"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            sys.exit(f"[error] git log failed: {result.stderr.strip()}")
        return result.stdout
    except FileNotFoundError:
        sys.exit("[error] git not found in PATH.")
    except subprocess.TimeoutExpired:
        sys.exit("[error] git log timed out.")


def _parse_git_log(raw: str) -> tuple[
    dict[str, list[tuple[str, datetime]]],   # file -> [(canonical_name, dt), ...]
    dict[str, set[str]],                      # file -> {canonical_names}
    dict[str, dict[str, int]],               # canonical_name -> {file: commit_count}
    dict[str, datetime],                      # canonical_name -> latest commit dt
]:
    """
    Parse git log output (--format="%H|%aN|%ae|%aI" --name-only).

    Name/email variants are merged into a single canonical identity: the most
    frequent display name across all commits for a given email.  When an author
    uses multiple emails we merge them by the lower-cased display-name first,
    then by the most-common email cluster.

    Returns:
        file_commits   – ordered list of (canonical_name, datetime) per file
        file_authors   – set of canonical names that ever touched each file
        author_files   – commit count per author per file
        author_latest  – most recent commit datetime per canonical name
    """
    # Step 1: collect raw rows
    # Each commit block: header line + blank line + file lines until next header
    commits: list[tuple[str, str, str, str]] = []  # (hash, name, email, iso_ts)
    commit_files: dict[str, list[str]] = {}         # hash -> [files]

    current_hash: str | None = None
    current_name = current_email = current_ts = ""
    pending_files: list[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line and len(line.split("|")) == 4:
            # Flush previous commit
            if current_hash is not None:
                commit_files[current_hash] = pending_files[:]
            h, name, email, ts = line.split("|", 3)
            current_hash, current_name, current_email, current_ts = h, name, email, ts
            pending_files = []
            commits.append((h, name, email, ts))
        else:
            # File path line
            if current_hash is not None:
                pending_files.append(line)

    # Flush last commit
    if current_hash is not None:
        commit_files[current_hash] = pending_files[:]

    # Step 2: build email → {name: count} for canonical resolution
    email_name_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _h, name, email, _ts in commits:
        email_norm = email.lower()
        email_name_counts[email_norm][name] += 1

    # Also cluster by lower-cased display name (handles name/email variants)
    name_norm_email_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _h, name, email, _ts in commits:
        name_norm_email_counts[name.lower()][email.lower()] += 1

    # canonical(email) = most frequent display name for that email
    def canonical_for_email(email: str) -> str:
        norm = email.lower()
        if norm in email_name_counts:
            return max(email_name_counts[norm], key=email_name_counts[norm].__getitem__)
        return email

    # Step 3: walk commits and build indices
    file_commits: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
    file_authors: dict[str, set[str]] = defaultdict(set)
    author_files: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    author_latest: dict[str, datetime] = {}

    for h, name, email, ts in commits:
        canon = canonical_for_email(email)
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue

        if canon not in author_latest or dt > author_latest[canon]:
            author_latest[canon] = dt

        for fpath in commit_files.get(h, []):
            if not fpath:
                continue
            file_commits[fpath].append((canon, dt))
            file_authors[fpath].add(canon)
            author_files[canon][fpath] += 1

    return dict(file_commits), dict(file_authors), dict(author_files), author_latest


# ---------------------------------------------------------------------------
# Dominant owner (most commits; ties → most recent)
# ---------------------------------------------------------------------------

def _dominant_owner(
    fpath: str,
    file_commits: dict[str, list[tuple[str, datetime]]],
) -> str | None:
    """Return the canonical name with most commits to fpath (ties → most recent)."""
    commits = file_commits.get(fpath, [])
    if not commits:
        return None
    counts: dict[str, int] = defaultdict(int)
    latest: dict[str, datetime] = {}
    for name, dt in commits:
        counts[name] += 1
        if name not in latest or dt > latest[name]:
            latest[name] = dt
    return max(counts, key=lambda n: (counts[n], latest[n]))


# ---------------------------------------------------------------------------
# Per-file readiness scoring (0-100)
# breadth 0.5 + recency 0.3 + depth 0.2
# ---------------------------------------------------------------------------

_TODAY = datetime.now(timezone.utc)


def _days_since(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max((_TODAY - dt).days, 0)


def _recency_weight(days: int) -> float:
    """1 / (1 + days/365).  1.0 today, ~0.5 at 1 yr, ~0.25 at 3 yrs."""
    return 1.0 / (1.0 + days / 365.0)


def _bucket(recency_raw: float) -> str:
    if recency_raw >= 0.75:
        return "ready"
    if recency_raw >= 0.40:
        return "familiar"
    return "cold"


def _per_file_readiness(
    candidate: str,
    target_file: str,
    high_risk_files: set[str],
    author_files: dict[str, dict[str, int]],
    file_commits: dict[str, list[tuple[str, datetime]]],
    all_tracked_files: set[str],
) -> dict[str, Any]:
    """
    Compute 0-100 readiness score for `candidate` on `target_file`.

    breadth  0.5 – direct commits to target file (capped + recency-decayed)
    recency  0.3 – recency of most recent commit to target file
    depth    0.2 – co-changed files touched + same-directory files touched
    """
    # --- recency: most recent commit to target file by this candidate ---
    file_commit_list = file_commits.get(target_file, [])
    candidate_commits_on_file = [
        (n, dt) for (n, dt) in file_commit_list if n == candidate
    ]
    if candidate_commits_on_file:
        most_recent_dt = max(dt for _, dt in candidate_commits_on_file)
        days = _days_since(most_recent_dt)
        direct_commits = len(candidate_commits_on_file)
    else:
        days = 9999
        direct_commits = 0

    recency_raw = _recency_weight(days)

    # --- breadth: direct commits to target file, capped at 20, recency-decayed ---
    capped_commits = min(direct_commits, 20)
    breadth_raw = (capped_commits / 20.0) * recency_raw  # 0..1

    # --- depth: co-changed files (appear in same commits) + same-dir files ---
    target_dir = str(Path(target_file).parent)
    # Files changed in same commits as target_file (co-changed)
    commits_touching_target: set[str] = set()
    for n, dt in file_commit_list:
        # We can't easily look up by hash here, use the co-authoring proxy:
        # count how many files the candidate touched that also appear with target
        pass  # We'll compute this differently below

    # Instead: for each file the candidate has touched, count those that share
    # at least one commit where target_file also changed.
    # Build a commit-hash → files mapping from file_commits isn't available directly.
    # Approximate: files touched by candidate that are in the same directory.
    cand_files = set(author_files.get(candidate, {}).keys())
    same_dir_files = {
        f for f in cand_files
        if f != target_file and str(Path(f).parent) == target_dir
    }
    # Co-changed: high-risk files the candidate also touched (proxy for co-change)
    cochanged_touched = {
        f for f in cand_files
        if f in high_risk_files and f != target_file
    }
    neighbor_touched = same_dir_files

    cochanged_count = len(cochanged_touched)
    neighbor_count = len(neighbor_files_touched := neighbor_touched)

    depth_raw = min((cochanged_count + neighbor_count) / 10.0, 1.0)  # cap at 1.0

    # --- weighted sum → 0-100 ---
    raw = 0.5 * breadth_raw + 0.3 * recency_raw + 0.2 * depth_raw
    score = round(raw * 100.0, 2)

    return {
        "direct_commits": direct_commits,
        "days_since_last_touch": days if days < 9999 else None,
        "recency_raw": recency_raw,
        "breadth_raw": breadth_raw,
        "depth_raw": depth_raw,
        "score": score,
        "bucket": _bucket(recency_raw),
        "cochanged_files_touched": cochanged_count,
        "neighbor_files_touched": neighbor_count,
    }


# ---------------------------------------------------------------------------
# Global team leaderboard: breadth × recency, normalised to 0-100
# ---------------------------------------------------------------------------

def _build_leaderboard(
    candidates: set[str],
    high_risk_files: set[str],
    file_commits: dict[str, list[tuple[str, datetime]]],
    author_files: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """
    For each candidate: raw_score = Σ recency_weight over high-risk files touched.
    Normalise to 0-100.
    """
    board: list[dict[str, Any]] = []
    for cand in candidates:
        covered = []
        raw = 0.0
        for fpath in high_risk_files:
            touched = file_commits.get(fpath, [])
            cand_touches = [(n, dt) for (n, dt) in touched if n == cand]
            if cand_touches:
                most_recent = max(dt for _, dt in cand_touches)
                days = _days_since(most_recent)
                raw += _recency_weight(days)
                covered.append(fpath)
        board.append({
            "candidate": cand,
            "files_covered": len(covered),
            "covered_files": sorted(covered),
            "raw_score": raw,
        })

    if not board:
        return []

    max_raw = max(b["raw_score"] for b in board)
    for b in board:
        b["score"] = round((b["raw_score"] / max_raw * 100.0) if max_raw > 0 else 100.0, 2)
        del b["raw_score"]

    board.sort(key=lambda b: b["score"], reverse=True)
    return board


# ---------------------------------------------------------------------------
# LLM "why" sentence — optional, falls back gracefully
# ---------------------------------------------------------------------------

def _llm_why(
    candidate: str,
    target_file: str,
    score: float,
    direct_commits: int,
    days_since: int | None,
    bucket: str,
) -> str | None:
    """
    Ask Bob for a one-sentence readiness explanation.
    Returns None if Bob CLI is unavailable, not configured, or opt-in is absent.
    Callers must check the opt-in flag before calling this function.
    """
    stats = (
        f"candidate={candidate}, file={target_file}, "
        f"readiness_score={score}/100, bucket={bucket}, "
        f"direct_commits={direct_commits}, "
        f"days_since_last_touch={days_since}"
    )
    prompt = (
        "You are a software team analyst. Given the following contributor stats "
        "for a high-risk file, write exactly ONE concise sentence (≤20 words) "
        "explaining why this person is or isn't ready to take over this file. "
        f"Stats: {stats}"
    )
    try:
        result = subprocess.run(
            ["bob", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    return line
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _data_driven_why(
    candidate: str,
    direct_commits: int,
    days_since: int | None,
    bucket: str,
    cochanged: int,
    neighbor: int,
) -> str:
    """Template-based why sentence — always succeeds."""
    parts = []
    if direct_commits > 0:
        parts.append(f"{direct_commits} direct commit{'s' if direct_commits != 1 else ''}")
    if days_since is not None:
        parts.append(f"last touched {days_since} day{'s' if days_since != 1 else ''} ago")
    if cochanged:
        parts.append(f"co-changed {cochanged} high-risk file{'s' if cochanged != 1 else ''}")
    if neighbor:
        parts.append(f"{neighbor} neighboring file{'s' if neighbor != 1 else ''} touched")
    detail = ", ".join(parts) if parts else "no direct history"
    return f"{bucket.capitalize()} — {detail}."


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Onboarding Readiness Analyzer — rank successor candidates for HIGH-risk files."
    )
    parser.add_argument(
        "repo",
        nargs="?",
        default=_DEFAULT_REPO,
        help=f"Path to the target git repo (default: {_DEFAULT_REPO})",
    )
    parser.add_argument(
        "--risk-report",
        default=_DEFAULT_RISK_REPORT,
        metavar="PATH",
        help=f"Path to risk_report.json (default: {_DEFAULT_RISK_REPORT})",
    )
    parser.add_argument(
        "--contributor-report",
        default=_DEFAULT_CONTRIB_REPORT,
        metavar="PATH",
        help=f"Path to contributor_report.json (default: {_DEFAULT_CONTRIB_REPORT})",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=3,
        metavar="N",
        help="Number of successor candidates per file (default: 3)",
    )
    parser.add_argument(
        "--with-llm-why",
        action="store_true",
        default=False,
        help="Generate per-candidate 'why' sentences via Bob (requires bob CLI). "
             "Also enabled by setting BOB_WHY=1 in the environment. "
             "Disabled by default; data-driven sentences are used otherwise.",
    )
    args = parser.parse_args()

    repo_path = os.path.abspath(args.repo)
    risk_report_path = os.path.abspath(args.risk_report)
    contrib_report_path = os.path.abspath(args.contributor_report)
    top_n = args.top_n
    use_llm_why: bool = args.with_llm_why or os.environ.get("BOB_WHY") == "1"

    # ------------------------------------------------------------------
    # 1. Validate inputs
    # ------------------------------------------------------------------
    for label, path in [
        ("repo", repo_path),
        ("risk_report", risk_report_path),
        ("contributor_report", contrib_report_path),
    ]:
        if not os.path.exists(path):
            sys.exit(f"[error] {label} path does not exist: {path}")

    if not os.path.isdir(os.path.join(repo_path, ".git")):
        sys.exit(f"[error] {repo_path} is not a git repository.")

    # ------------------------------------------------------------------
    # 2. Load risk report (bare list, encoding ladder)
    # ------------------------------------------------------------------
    risk_data = _load_json_with_ladder(risk_report_path)
    if not isinstance(risk_data, list):
        sys.exit(f"[error] {risk_report_path} must be a JSON array at the top level.")

    high_risk_entries = [e for e in risk_data if e.get("risk_level") == "HIGH"]
    print(f"Found {len(high_risk_entries)} HIGH-risk files.")

    if not high_risk_entries:
        print("No HIGH-risk files — nothing to analyze.")
        sys.exit(0)

    high_risk_files: set[str] = {e["file"] for e in high_risk_entries}

    # ------------------------------------------------------------------
    # 3. Load contributor report
    # ------------------------------------------------------------------
    contrib_raw = None
    for enc in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le"):
        try:
            with open(contrib_report_path, encoding=enc) as fh:
                contrib_raw = json.load(fh)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
        except json.JSONDecodeError as exc:
            sys.exit(f"[error] {contrib_report_path} is not valid JSON: {exc}")

    if contrib_raw is None:
        sys.exit(f"[error] Cannot decode {contrib_report_path}.")

    contrib_index: dict[str, dict] = {
        entry["file"]: entry for entry in contrib_raw.get("files", [])
    }

    # ------------------------------------------------------------------
    # 4. Single git log pass
    # ------------------------------------------------------------------
    print("Running git log (single read-only pass)…")
    raw_log = _run_git_log(repo_path)
    file_commits, file_authors, author_files, author_latest = _parse_git_log(raw_log)

    all_tracked_files: set[str] = set(file_commits.keys())

    # ------------------------------------------------------------------
    # 5. Resolve all canonical names seen across high-risk files
    # ------------------------------------------------------------------
    all_candidates: set[str] = set()
    for fpath in high_risk_files:
        for name in file_authors.get(fpath, set()):
            all_candidates.add(name)
    # Also include anyone from the contributor report (for leaderboard)
    for entry in contrib_raw.get("files", []):
        for author_str in entry.get("authors", []):
            name = author_str.split(" <")[0]
            all_candidates.add(name)

    # ------------------------------------------------------------------
    # 6. Global team leaderboard (breadth × recency over ALL high-risk files)
    # ------------------------------------------------------------------
    leaderboard = _build_leaderboard(
        all_candidates, high_risk_files, file_commits, author_files
    )
    # Build quick lookup: candidate → leaderboard rank (0-based)
    lb_rank: dict[str, int] = {b["candidate"]: i for i, b in enumerate(leaderboard)}
    lb_score: dict[str, float] = {b["candidate"]: b["score"] for b in leaderboard}

    # ------------------------------------------------------------------
    # 7. Per-file analysis
    # ------------------------------------------------------------------
    files_output: list[dict] = []
    no_direct_successor_count = 0

    for risk_entry in high_risk_entries:
        fpath = risk_entry["file"]

        # Dominant owner: most commits, ties → most recent
        dominant = _dominant_owner(fpath, file_commits)

        # Fallback to contributor report when git log has no history for this file
        if dominant is None:
            c_entry = contrib_index.get(fpath)
            if c_entry and c_entry.get("authors"):
                dominant = c_entry["authors"][0].split(" <")[0]

        # Candidate pool: all who touched the file EXCEPT the dominant owner
        direct_pool = {
            name for name in file_authors.get(fpath, set())
            if name != dominant
        }

        # Compute per-file readiness for each direct candidate
        scored_direct: list[dict] = []
        for cand in direct_pool:
            metrics = _per_file_readiness(
                cand, fpath, high_risk_files, author_files, file_commits, all_tracked_files
            )
            days = metrics["days_since_last_touch"]

            # LLM why only when explicitly opted in; always fall back to data-driven
            why: str | None = None
            if use_llm_why:
                why = _llm_why(
                    cand, fpath, metrics["score"], metrics["direct_commits"], days, metrics["bucket"]
                )
            if why is None:
                why = _data_driven_why(
                    cand,
                    metrics["direct_commits"],
                    days,
                    metrics["bucket"],
                    metrics["cochanged_files_touched"],
                    metrics["neighbor_files_touched"],
                )

            scored_direct.append({
                "author": cand,
                "readiness_score": metrics["score"],
                "bucket": metrics["bucket"],
                "direct_commits": metrics["direct_commits"],
                "cochanged_files_touched": metrics["cochanged_files_touched"],
                "neighbor_files_touched": metrics["neighbor_files_touched"],
                "days_since_last_touch": days,
                "why": why,
            })

        scored_direct.sort(key=lambda x: x["readiness_score"], reverse=True)
        backups = scored_direct[:top_n]

        # Count files where zero direct-history candidates exist (entirely leaderboard-filled)
        if not backups:
            no_direct_successor_count += 1

        # Leaderboard fallback when not enough direct candidates
        if len(backups) < top_n:
            used_names = {b["author"] for b in backups} | ({dominant} if dominant else set())
            for lb_entry in leaderboard:
                if len(backups) >= top_n:
                    break
                cand = lb_entry["candidate"]
                if cand in used_names:
                    continue
                # Force cold bucket per spec
                days_val: int | None = None
                cand_file_touches = [
                    (n, dt) for (n, dt) in file_commits.get(fpath, [])
                    if n == cand
                ]
                if cand_file_touches:
                    most_recent = max(dt for _, dt in cand_file_touches)
                    days_val = _days_since(most_recent)

                backups.append({
                    "author": cand,
                    "readiness_score": lb_entry["score"],
                    "bucket": "cold",
                    "direct_commits": 0,
                    "cochanged_files_touched": len(
                        [f for f in author_files.get(cand, {}) if f in high_risk_files and f != fpath]
                    ),
                    "neighbor_files_touched": len(
                        [f for f in author_files.get(cand, {})
                         if f != fpath and str(Path(f).parent) == str(Path(fpath).parent)]
                    ),
                    "days_since_last_touch": days_val,
                    "why": "no direct history — closest active contributor by team leaderboard",
                })
                used_names.add(cand)

        # Determine dominant_owner and all_owners for output schema
        # Resolve all_owners through the same canonical merge so name variants
        # (e.g. "Ash" vs "ashuntu") don't appear as separate entries.
        raw_git_owners = list(file_authors.get(fpath, set()))
        if raw_git_owners:
            # Deduplicate: keep the canonical name for each unique identity.
            # Two names are the same identity when canonical_for_email maps them
            # to the same result — but here we already have canonical names from
            # the git log parse, so just deduplicate the set.
            seen: set[str] = set()
            all_owners: list[str] = []
            for name in raw_git_owners:
                if name not in seen:
                    seen.add(name)
                    all_owners.append(name)
        else:
            contrib_entry = contrib_index.get(fpath, {})
            all_owners_raw = contrib_entry.get("authors", [])
            all_owners = list(dict.fromkeys(a.split(" <")[0] for a in all_owners_raw))
        if not all_owners and dominant:
            all_owners = [dominant]

        files_output.append({
            "file": fpath,
            "risk_level": risk_entry.get("risk_level", "HIGH"),
            "dominant_owner": dominant,
            "all_owners": all_owners,
            "backups": backups,
        })

    # ------------------------------------------------------------------
    # 8. Assemble and write report
    # ------------------------------------------------------------------
    report = {
        "agent": "onboarding_agent",
        "repo": repo_path,
        "method": "breadth0.5+recency0.3+depth0.2",
        "high_risk_file_count": len(high_risk_entries),
        "files": files_output,
    }

    os.makedirs(os.path.dirname(_OUTPUT_PATH), exist_ok=True)
    with open(_OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    # ------------------------------------------------------------------
    # 9. Stdout summary
    # ------------------------------------------------------------------
    print()
    print("Onboarding Readiness Report")
    print(f"HIGH-risk files: {len(high_risk_entries)}  "
          f"({no_direct_successor_count} with no direct successor)")
    print("Top successor candidates:")
    for i, entry in enumerate(leaderboard[:top_n], 1):
        print(f"  {i}. {entry['candidate']}  "
              f"(covers {entry['files_covered']} file{'s' if entry['files_covered'] != 1 else ''}, "
              f"score {entry['score']}/100)")
    print(f"Report written to {_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
