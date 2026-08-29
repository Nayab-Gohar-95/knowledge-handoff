"""
orchestrator.py — Knowledge Continuity Suite
IBM TechXchange 2026 Hackathon

Merges output from the three subagents (Contributor Analysis, Complexity/
Criticality, Documentation Gap) into a single ranked risk report.

Public surface:
    compute_risk(files)         — main ranking function
    generate_reason_llm(f)      — LLM-backed reason via Bob Shell
    demo(repo_path)             — standalone end-to-end demo on a local repo

No external dependencies beyond the Python standard library.
"""

from __future__ import annotations

import ast
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

_RISK_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

# Keywords that indicate branching / control-flow complexity
_BRANCH_KEYWORDS = re.compile(
    r"\b(if|elif|else|for|while|except|case|switch)\b|(\|\|)|(&&)"
)


def _classify(
    author_count: int,
    complexity_score: float,
    doc_score: float,
    median_complexity: float,
) -> tuple[str, list[str]]:
    """Return (risk_level, list_of_triggered_conditions)."""
    high_complexity = complexity_score > median_complexity
    low_doc = doc_score < 40
    few_authors = author_count <= 2

    if few_authors and high_complexity and low_doc:
        conditions = []
        if high_complexity:
            conditions.append("above-median complexity")
        if low_doc:
            conditions.append(f"doc score {doc_score:.1f}/100")
        return "HIGH", conditions

    if few_authors and (high_complexity or low_doc):
        conditions = []
        if high_complexity:
            conditions.append("above-median complexity")
        if low_doc:
            conditions.append(f"doc score {doc_score:.1f}/100")
        return "MEDIUM", conditions

    return "LOW", []


# ---------------------------------------------------------------------------
# Reason generation — template-based (fast, free)
# ---------------------------------------------------------------------------

def _generate_reason(f: dict[str, Any]) -> str:
    """
    Build a one-line plain-English explanation using only the dict fields.
    No external calls; always succeeds.
    """
    author_count: int = f["author_count"]
    last_touch: int = f["last_touch_days_ago"]
    doc: float = f["doc_score"]

    author_str = (
        f"{author_count} contributor"
        if author_count == 1
        else f"{author_count} contributors"
    )
    touch_str = f"last touched {last_touch} day{'s' if last_touch != 1 else ''} ago"
    doc_str = (
        "zero comments (doc score 0.0/100)"
        if doc == 0
        else f"doc score {doc:.1f}/100"
    )

    return f"{author_str}, {touch_str}, {doc_str}."


# ---------------------------------------------------------------------------
# Reason generation — LLM-backed via Bob Shell
# ---------------------------------------------------------------------------

def generate_reason_llm(f: dict[str, Any]) -> str:
    """
    Ask IBM Bob (non-interactive mode) for a one-sentence natural-language
    explanation of why the file is risky.

    Invokes: bob -p "<prompt>"

    Falls back to _generate_reason(f) on any subprocess error or timeout.
    """
    stats = (
        f"file={f['file']}, "
        f"author_count={f['author_count']}, "
        f"last_touch_days_ago={f['last_touch_days_ago']}, "
        f"complexity_score={f['complexity_score']:.1f}, "
        f"doc_score={f['doc_score']:.1f}/100, "
        f"risk_level={f.get('risk_level', 'UNKNOWN')}"
    )
    prompt = (
        "You are a software risk analyst. Given the following file stats, "
        "write exactly ONE concise sentence (≤20 words) explaining why this "
        f"file is a knowledge-continuity risk. Stats: {stats}"
    )

    try:
        result = subprocess.run(
            ["bob", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=15,  # seconds; generous for a single sentence
        )
        if result.returncode == 0:
            # Grab the first non-empty line from Bob's response
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    return line
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass  # Bob CLI unavailable or timed out — fall back below

    return _generate_reason(f)


# ---------------------------------------------------------------------------
# Main orchestrator function
# ---------------------------------------------------------------------------

def compute_risk(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merge subagent outputs into a ranked risk report.

    Each input dict must contain:
        file               (str)   — file path
        author_count       (int)   — distinct git authors
        last_touch_days_ago(int)   — days since last commit
        complexity_score   (float) — higher = more complex
        doc_score          (float) — 0–100, higher = better documented

    Returns the same dicts enriched with:
        risk_level  (str)   — "HIGH" | "MEDIUM" | "LOW"
        why         (str)   — one-line plain-English reason

    Sorted HIGH → MEDIUM → LOW; ties broken by ascending doc_score.
    """
    if not files:
        return []

    med_complexity = median(f["complexity_score"] for f in files)

    enriched: list[dict[str, Any]] = []
    for f in files:
        risk_level, _conditions = _classify(
            author_count=f["author_count"],
            complexity_score=f["complexity_score"],
            doc_score=f["doc_score"],
            median_complexity=med_complexity,
        )
        enriched_f = dict(f)  # shallow copy — don't mutate caller's data
        enriched_f["risk_level"] = risk_level
        enriched_f["why"] = _generate_reason(enriched_f)
        enriched.append(enriched_f)

    enriched.sort(key=lambda x: (_RISK_ORDER[x["risk_level"]], x["doc_score"]))
    return enriched


# ---------------------------------------------------------------------------
# Demo / standalone end-to-end runner
# ---------------------------------------------------------------------------

def _git_file_stats(repo: Path, filepath: str) -> tuple[int, int]:
    """
    Return (author_count, last_touch_days_ago) for *filepath* inside *repo*
    by parsing `git log --follow --format="%an|%ad" --date=short -- <file>`.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--format=%an|%ad", "--date=short", "--", filepath],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 1, 0

    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    if not lines:
        return 1, 0

    authors: set[str] = set()
    dates: list[date] = []
    for line in lines:
        parts = line.split("|", 1)
        if len(parts) == 2:
            authors.add(parts[0].strip())
            try:
                dates.append(datetime.strptime(parts[1].strip(), "%Y-%m-%d").date())
            except ValueError:
                pass

    author_count = max(len(authors), 1)
    last_touch = (date.today() - max(dates)).days if dates else 0
    return author_count, last_touch


def _complexity_score(source: str) -> float:
    """
    Rough complexity = LOC + count of branching keywords / operators.
    """
    loc = len([l for l in source.splitlines() if l.strip()])
    branches = len(_BRANCH_KEYWORDS.findall(source))
    return float(loc + branches)


def _doc_score_py(source: str) -> float:
    """
    For .py files: use ast to count docstrings + comment lines.
    Returns 0–100.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _doc_score_generic(source)  # fall back to comment-line heuristic

    total_nodes = 0
    documented_nodes = 0

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            total_nodes += 1
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                documented_nodes += 1

    docstring_density = (documented_nodes / total_nodes * 50) if total_nodes else 0

    lines = source.splitlines()
    comment_lines = sum(1 for l in lines if l.strip().startswith("#"))
    code_lines = max(len([l for l in lines if l.strip()]), 1)
    comment_density = min(comment_lines / code_lines * 50, 50)

    return round(docstring_density + comment_density, 1)


def _doc_score_generic(source: str) -> float:
    """
    For non-.py files: ratio of comment lines (// or #) to total code lines,
    mapped to 0–100.
    """
    lines = source.splitlines()
    code_lines = [l for l in lines if l.strip()]
    if not code_lines:
        return 0.0
    comment_lines = [
        l for l in code_lines if l.strip().startswith(("//", "#", "/*", "*"))
    ]
    return round(min(len(comment_lines) / len(code_lines) * 100, 100), 1)


def demo(repo_path: str) -> None:
    """
    Standalone demo: scan a local git repo, extract metrics for all tracked
    .py / .js / .ts files, run them through compute_risk(), and print a table.

    Usage:
        python orchestrator.py /path/to/your/repo
    """
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        print(f"[error] {repo} does not appear to be a git repository.")
        return

    # List all tracked files
    try:
        ls_result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[error] Could not list git files: {exc}")
        return

    tracked = [
        p for p in ls_result.stdout.splitlines()
        if p.endswith((".py", ".js", ".ts"))
    ]

    if not tracked:
        print("[info] No .py/.js/.ts files found in this repository.")
        return

    print(f"[info] Scanning {len(tracked)} file(s) in {repo} …\n")

    file_dicts: list[dict[str, Any]] = []
    for filepath in tracked:
        abs_path = repo / filepath
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""

        author_count, last_touch_days_ago = _git_file_stats(repo, filepath)
        complexity = _complexity_score(source)
        doc = (
            _doc_score_py(source)
            if filepath.endswith(".py")
            else _doc_score_generic(source)
        )

        file_dicts.append(
            {
                "file": filepath,
                "author_count": author_count,
                "last_touch_days_ago": last_touch_days_ago,
                "complexity_score": complexity,
                "doc_score": doc,
            }
        )

    ranked = compute_risk(file_dicts)

    # Print ranked table
    col_risk       = 6
    col_file       = max(len(r["file"]) for r in ranked) + 2
    col_complexity = 12  # "COMPLEXITY" header is 10 chars

    header = (
        f"{'RISK':<{col_risk}}  {'FILE':<{col_file}}  "
        f"{'COMPLEXITY':>{col_complexity}}  WHY"
    )
    print(header)
    print("-" * len(header))
    for r in ranked:
        complexity_str = f"{r['complexity_score']:.1f}"
        print(
            f"{r['risk_level']:<{col_risk}}  {r['file']:<{col_file}}  "
            f"{complexity_str:>{col_complexity}}  {r['why']}"
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python orchestrator.py <path-to-git-repo>")
        sys.exit(1)

    demo(sys.argv[1])
