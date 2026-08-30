"""
extraction_agent.py -- Knowledge Continuity Suite
Module 3: Knowledge-Extraction Assistant

Takes every HIGH-risk file from risk_report.json and auto-drafts a plain-English
knowledge document for it, using three sources of evidence that already exist in
the repository:
  1. Git commit messages for that file (subject + body)
  2. Inline code comments and docstrings extracted from the file itself
  3. Risk metadata (complexity, doc score, last-touch age) from the risk report

Output is a DRAFT for a human to review -- not a finished spec.

Inputs:
    outputs/risk_report.json         -- risk levels; HIGH files are targeted
    outputs/contributor_report.json  -- owner names + commit counts
    <repo_path>                      -- local git clone to read file contents
                                        (read from contributor_report["repo"])

Output:
    outputs/extraction_report.json   -- structured drafts (JSON)
    outputs/extraction_drafts/       -- one <sanitised-filename>.md per file

Usage (run from backend/):
    python agents/extraction_agent.py \\
        outputs/risk_report.json \\
        outputs/contributor_report.json
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers: git data
# ---------------------------------------------------------------------------

def _git_commit_messages(repo: Path, filepath: str, limit: int = 20) -> list[dict[str, str]]:
    """Return up to *limit* commit subjects + bodies for *filepath*."""
    try:
        result = subprocess.run(
            ["git", "log", "--follow", f"--max-count={limit}",
             "--format=SUBJECT:%s%nBODY:%b%nEND_COMMIT", "--", filepath],
            cwd=repo, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    commits: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("SUBJECT:"):
            current = {"subject": line[len("SUBJECT:"):].strip(), "body": ""}
        elif line.startswith("BODY:"):
            current["body"] = line[len("BODY:"):].strip()
        elif line == "END_COMMIT" and current:
            commits.append(current)
            current = {}
    return commits


def _read_file_from_repo(repo: Path, filepath: str) -> str:
    """Read the HEAD revision of *filepath* from the repo."""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{filepath}"],
            cwd=repo, capture_output=True, text=True, errors="replace", timeout=10,
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


# ---------------------------------------------------------------------------
# Helpers: comment / docstring extraction
# ---------------------------------------------------------------------------

_PY_DOCSTRING = re.compile(r'"""(.*?)"""', re.DOTALL)
_PY_COMMENT   = re.compile(r"^\s*#\s*(.+)$", re.MULTILINE)
_SH_COMMENT   = re.compile(r"^\s*#\s*(.+)$", re.MULTILINE)
_JS_COMMENT   = re.compile(r"//\s*(.+)$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HTML_COMMENT  = re.compile(r"<!--.*?-->", re.DOTALL)
_YAML_COMMENT  = re.compile(r"^\s*#\s*(.+)$", re.MULTILINE)


def _extract_comments(source: str, filepath: str) -> list[str]:
    """Return a list of unique non-trivial comment/docstring strings."""
    ext = Path(filepath).suffix.lower()
    raw: list[str] = []

    if ext == ".py":
        raw += [m.strip() for m in _PY_DOCSTRING.findall(source)]
        raw += _PY_COMMENT.findall(source)
    elif ext in (".js", ".ts"):
        raw += [m.strip() for m in _BLOCK_COMMENT.findall(source)]
        raw += _JS_COMMENT.findall(source)
    elif ext in (".sh", "") and "/" in filepath:  # shell scripts often have no ext
        raw += _SH_COMMENT.findall(source)
    elif ext in (".html", ".htm"):
        raw += [m.strip() for m in _HTML_COMMENT.findall(source)]
    elif ext in (".yaml", ".yml", ".json", ".md", ".txt", ".css"):
        raw += _YAML_COMMENT.findall(source)

    # Deduplicate and drop very short / boilerplate lines
    seen: set[str] = set()
    result: list[str] = []
    for c in raw:
        c = c.strip()
        if len(c) < 8:
            continue
        if c in seen:
            continue
        seen.add(c)
        result.append(c)

    return result[:30]   # cap at 30 to keep the draft readable


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

def _build_draft(
    filepath: str,
    risk_entry: dict[str, Any],
    contributor_rec: dict[str, Any] | None,
    commits: list[dict[str, str]],
    comments: list[str],
) -> str:
    """
    Produce a Markdown draft for human review.
    Template-driven — no LLM call, works offline and in any demo environment.
    """
    owners = []
    if contributor_rec:
        for raw in contributor_rec.get("authors", []):
            name = re.sub(r"\s*<[^>]*>", "", raw).strip()
            if name:
                owners.append(name)
    owners_str = ", ".join(owners) if owners else "Unknown"
    commit_count = contributor_rec.get("commit_count", "?") if contributor_rec else "?"
    last_touch = contributor_rec.get("last_touch_date", "unknown") if contributor_rec else "unknown"

    complexity = risk_entry.get("complexity_score", "?")
    doc_score  = risk_entry.get("doc_score", "?")
    why        = risk_entry.get("why", "")

    # Commit history summary
    commit_lines: list[str] = []
    for c in commits[:10]:
        subj = c["subject"]
        body = c["body"].strip()
        line = f"- **{subj}**"
        if body:
            line += f" — {body[:120]}"
        commit_lines.append(line)
    commit_section = "\n".join(commit_lines) if commit_lines else "_No commit history found._"

    # Comments/docstrings section
    if comments:
        comment_section = "\n".join(f"- {c[:200]}" for c in comments[:15])
    else:
        comment_section = "_No inline comments or docstrings found in this file._"

    draft = f"""# Knowledge Draft: `{filepath}`

> ⚠️ **DRAFT — for human review.** Auto-generated from git history and code
> comments. Verify accuracy before sharing.

---

## File at a Glance

| Field | Value |
|---|---|
| **Path** | `{filepath}` |
| **Primary owner(s)** | {owners_str} |
| **Total commits** | {commit_count} |
| **Last modified** | {last_touch} |
| **Complexity score** | {complexity} |
| **Documentation score** | {doc_score}/100 |
| **Risk assessment** | {why} |

---

## What This File Does (inferred from commit history)

The following commit messages describe how this file has evolved.
Use them to reconstruct the file's purpose and key design decisions.

{commit_section}

---

## Key Concepts (extracted from inline comments and docstrings)

{comment_section}

---

## Suggested Questions for the Owner

Before the owner leaves, ask them to clarify:

1. What is the single most important thing this file does?
2. What would break first if this file was deleted or corrupted?
3. Are there undocumented dependencies or assumptions a new maintainer must know?
4. What is the safest way to test a change to this file?
5. Is there any planned work on this file that hasn't been committed yet?

---

_Generated by extraction_agent.py — Knowledge Continuity Suite_
"""
    return draft.strip()


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze(
    risk_report: list[dict[str, Any]],
    contributor_report: dict[str, Any],
    repo: Path,
) -> list[dict[str, Any]]:
    """Return one extraction record per HIGH-risk file."""
    # Index contributor records by file path for O(1) lookup
    contributor_index: dict[str, dict[str, Any]] = {
        rec["file"]: rec for rec in contributor_report.get("files", [])
    }

    results: list[dict[str, Any]] = []

    for entry in risk_report:
        if entry.get("risk_level") != "HIGH":
            continue

        fp = entry["file"]
        contributor_rec = contributor_index.get(fp)

        source = _read_file_from_repo(repo, fp)
        commits = _git_commit_messages(repo, fp)
        comments = _extract_comments(source, fp)

        draft_md = _build_draft(fp, entry, contributor_rec, commits, comments)

        results.append({
            "file": fp,
            "risk_level": "HIGH",
            "why": entry.get("why", ""),
            "commit_count_used": len(commits),
            "comment_count_extracted": len(comments),
            "draft_markdown": draft_md,
        })

    return results


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Any:
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return json.load(fh)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"Cannot decode {path}")


def _safe_filename(filepath: str) -> str:
    """Turn a repo-relative path into a safe filename stem."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", filepath)


def main() -> None:
    if len(sys.argv) not in (3, 4):
        print(
            "Usage: python agents/extraction_agent.py "
            "<risk_report.json> <contributor_report.json> [repo_path]",
            file=sys.stderr,
        )
        sys.exit(1)

    risk_report        = _load_json(sys.argv[1])
    contributor_report = _load_json(sys.argv[2])

    # repo_path can be overridden on the CLI (useful when running on a different
    # machine from where contributor_report.json was generated)
    repo_path_str = sys.argv[3] if len(sys.argv) == 4 else contributor_report.get("repo", "")
    if not repo_path_str:
        print("[error] Pass the repo path as a third argument or ensure "
              "contributor_report.json contains a 'repo' field", file=sys.stderr)
        sys.exit(1)

    repo = Path(repo_path_str)
    if not (repo / ".git").exists():
        print(f"[error] '{repo}' is not a git repository", file=sys.stderr)
        sys.exit(1)

    results = analyze(risk_report, contributor_report, repo)

    os.makedirs("outputs", exist_ok=True)
    os.makedirs(os.path.join("outputs", "extraction_drafts"), exist_ok=True)

    # Write individual Markdown drafts
    for rec in results:
        fname = _safe_filename(rec["file"]) + ".md"
        md_path = os.path.join("outputs", "extraction_drafts", fname)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(rec["draft_markdown"])

    # Write structured JSON report
    out_path = os.path.join("outputs", "extraction_report.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({
            "agent": "extraction_agent",
            "repo": repo_path_str,
            "high_risk_files_drafted": len(results),
            "files": results,
        }, fh, indent=2, ensure_ascii=False)

    print(f"Drafted {len(results)} knowledge document(s)")
    print(f"  JSON  -> {out_path}")
    print(f"  Markdown -> outputs/extraction_drafts/")
    print()
    for rec in results:
        print(f"  [{rec['risk_level']}] {rec['file']}  "
              f"({rec['commit_count_used']} commits, {rec['comment_count_extracted']} comments extracted)")


if __name__ == "__main__":
    main()
