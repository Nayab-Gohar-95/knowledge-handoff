# Onboarding Readiness Analyzer — Plan

## Overview

`onboarding_agent.py` is a read-only Python script that answers one question:
**"If the sole owner of a HIGH-risk file leaves tomorrow, who on the team is most
ready to take over?"**

It reuses the two already-generated reports — `backend/outputs/risk_report.json`
and `backend/outputs/contributor_report.json` — and the git history of
`sample-repos/steam-snap` (read-only). No new sub-agents, no network calls, no
writes to the sample repo.

**Core output:** A ranked list of successor candidates per HIGH-risk file, plus a
team-level "breadth × recency" readiness leaderboard, written to
`backend/outputs/onboarding_report.json` and summarised to stdout.

**Scope constraints:**
- Read-only: never writes to `sample-repos/steam-snap`.
- No new dependencies beyond `gitpython` (already in `requirements.txt`).
- No LLM calls; all reasoning is deterministic scoring.
- Does not re-run the upstream agents; treats the JSON reports as the source of truth.

---

## Data Flow

```
risk_report.json          contributor_report.json     git log (steam-snap, read-only)
       │                          │                             │
       ▼                          ▼                             ▼
  Filter HIGH-risk files     Build per-file             Per-candidate: days since
                             author roster              last commit to each file
                                   │                             │
                                   └──────────── join ───────────┘
                                                  │
                                      Score each candidate:
                                      breadth × recency weight
                                                  │
                                      ┌───────────────────────┐
                                      │  onboarding_report    │
                                      │  .json + stdout       │
                                      └───────────────────────┘
```

---

## Sub-Task 1 — Load and filter HIGH-risk files

**Status:** `[ ] pending`

**Intent:**
Parse `risk_report.json` and extract only the files whose `risk_level` is `"HIGH"`.
These are the files that represent a knowledge continuity threat and need successor
analysis. Keeping this step isolated makes it easy to later adjust the filter
(e.g. include MEDIUM) without touching scoring logic.

**Expected Outcomes:**
- A Python list of dicts, each being a full `risk_report.json` row with
  `risk_level == "HIGH"`.
- The list is available as the input set for sub-task 2.

**Todo List:**
1. Open `backend/outputs/risk_report.json` (path relative to the script's CWD or
   passed as a CLI arg; default to the sibling `outputs/` directory).
2. Parse the JSON array.
3. Filter to entries where `entry["risk_level"] == "HIGH"`.
4. Store the filtered list; log the count to stdout
   (`"Found N HIGH-risk files."`).

**Relevant Context:**
- `risk_report.json` is a flat JSON array (not wrapped in an envelope object).
  Schema per entry: `{ file, author_count, last_touch_days_ago, complexity_score,
  doc_score, risk_level, why }`.
- File paths are repo-relative strings (e.g. `"docs/.sphinx/_templates/footer.html"`).

---

## Sub-Task 2 — Build the candidate roster from contributor data

**Status:** `[ ] pending`

**Intent:**
For each HIGH-risk file, determine which contributors have ever touched it by
joining the HIGH-risk file list against `contributor_report.json`. This gives us
the pool of candidates who have hands-on experience with the file.

**Expected Outcomes:**
- A dict mapping each HIGH-risk file path → list of contributor name strings
  (extracted from the `authors` field of `contributor_report.json`).
- Files that appear in `risk_report.json` but not in `contributor_report.json`
  are noted in stderr and skipped (should not happen with current data but must
  be handled gracefully).

**Todo List:**
1. Open `backend/outputs/contributor_report.json`.
2. Build an index: `{ file_path: { "authors": [...], "commit_count": int,
   "days_since_last_touch": int } }` keyed by the `file` field.
3. For each HIGH-risk file, look up the index. If missing, warn to stderr and
   skip.
4. Parse the `authors` array — entries are in `"Name <email1, email2>"` format;
   extract the name portion (everything before ` <`) as the canonical identifier.

**Relevant Context:**
- `contributor_report.json` envelope: `{ agent, repo, file_count_analyzed,
  files: [...] }`. The per-file array is under `"files"`.
- Author strings are already deduplicated by name in `contributor_agent.py`
  (emails merged under one name entry). Name extraction: `name = author.split(" <")[0]`.

---

## Sub-Task 3 — Score candidates with breadth × recency

**Status:** `[ ] pending`

**Intent:**
Rank candidates by how well they could take over if the sole owner of any
HIGH-risk file left. The scoring metric is **breadth × recency**:

- **Breadth** = the number of distinct HIGH-risk files a candidate has touched.
- **Recency** = a time-decay weight derived from how recently they last committed
  to each such file, computed from git history.

This rewards candidates who have touched many HIGH-risk files recently, rather
than someone who touched one file once years ago.

**Expected Outcomes:**
- A candidate leaderboard: list of `{ candidate, files_covered, score (0–100),
  covered_files: [...] }` sorted by `score` descending. Score is normalised to
  0–100 so it speaks the same language as the per-file readiness buckets.
- Per HIGH-risk file: a ranked list of up to 3 successor candidates with their
  individual file-level recency score (also 0–100) and a `readiness` bucket label.

**Todo List:**
1. Open the `sample-repos/steam-snap` repo with `git.Repo(repo_path, odbt=git.GitCmdObjectDB)`
   in read-only mode (no writes).
2. For each `(candidate, high_risk_file)` pair from sub-task 2, query git log to
   find the candidate's most recent commit date on that file:
   ```
   git log --follow --author="<name>" --format="%aI" -- <file>
   ```
   Take the first (most recent) date. If the candidate has no commits on that file
   in git history (edge case), fall back to `days_since = days_since_last_touch`
   from the contributor report.
3. Compute per-file recency weight (raw, unbounded sum):
   `recency = 1 / (1 + days_since_last_commit / 365)`
   (yields 1.0 if touched today, ~0.5 at 1 year, ~0.25 at 3 years)
4. Assign each per-file recency score a **readiness bucket**:
   - `"ready"` — recency ≥ 0.75  (touched within ~4 months)
   - `"familiar"` — recency ≥ 0.40  (touched within ~18 months)
   - `"cold"` — recency < 0.40  (touched > 18 months ago)
5. Accumulate candidate raw totals:
   - `files_covered` = count of distinct HIGH-risk files touched
   - `raw_score` = sum of per-file recency weights across all covered HIGH-risk files
6. Normalise leaderboard scores to 0–100:
   `score = round((raw_score / max_raw_score) * 100, 2)`
   where `max_raw_score` is the highest raw total across all candidates.
   If only one candidate exists, their score is 100.
7. Sort candidates by `score` descending for the leaderboard.
8. For each HIGH-risk file, build its own ranked successor list (top 3 by
   per-file recency score on that file, converted to 0–100 the same way).

**Relevant Context:**
- Git log author filter `--author` matches by name substring; use the exact name
  string extracted in sub-task 2.
- `git.Repo` opened read-only does not lock or modify the repo; `repo.git.log()`
  is a passthrough subprocess call.
- `days_since_last_commit` is computed as `(today - parsed_date).days` using
  `datetime.fromisoformat()`.

---

## Sub-Task 4 — Assemble and write the output report

**Status:** `[ ] pending`

**Intent:**
Combine the HIGH-risk file list with per-file successor rankings and the team
leaderboard into a single structured JSON report, and print a human-readable
summary to stdout. The output mirrors the envelope format used by the other agents
so it is consistent with the rest of the pipeline.

**Expected Outcomes:**
- `backend/outputs/onboarding_report.json` is created (or overwritten) with the
  following schema:
  ```json
  {
    "agent": "onboarding_agent",
    "repo": "<absolute path to steam-snap>",
    "high_risk_file_count": <int>,
    "team_leaderboard": [
      {
        "candidate": "<name>",
        "files_covered": <int>,
        "score": <float 0-100, 2 dp>,
        "covered_files": ["<file>", ...]
      }
    ],
    "files": [
      {
        "file": "<repo-relative path>",
        "risk_level": "HIGH",
        "why": "<from risk_report>",
        "sole_owner": "<name or null>",
        "successors": [
          {
            "candidate": "<name>",
            "recency_score": <float 0-100, 2 dp>,
            "readiness": "ready | familiar | cold",
            "source": "direct_history | leaderboard_fallback"
          }
        ]
      }
    ]
  }
  ```
- `sole_owner` is the name from the contributor report when `author_count == 1`,
  otherwise `null`.
- `successors` contains up to 3 entries, sorted by `recency_score` descending.
  The `source` field is `"direct_history"` when the candidate has git commits on
  that exact file; `"leaderboard_fallback"` when the candidate was pulled from the
  team leaderboard because no direct contributor exists other than the sole owner.
- Leaderboard-fallback entries are **always bucketed `"cold"`** regardless of their
  overall leaderboard score, and carry a `"why"` field on the successor object:
  `"no direct history — closest active contributor by team leaderboard"`.
  This prevents a high leaderboard score from masquerading as file familiarity.
- Stdout summary:
  ```
  Onboarding Readiness Report
  HIGH-risk files: N  (X with no direct successor)
  Top successor candidates:
    1. <name>  (covers M files, score S/100)
    2. ...
  Report written to backend/outputs/onboarding_report.json
  ```
  The `(X with no direct successor)` count surfaces files relying entirely on
  leaderboard fallbacks — a key demo beat.

**Todo List:**
1. Build the `"files"` array by merging data from sub-tasks 1–3.
2. Set `sole_owner` from `author_count == 1` check in contributor data.
3. For files where no candidate other than the sole owner has direct git history,
   populate `successors` from the top 3 of the team leaderboard. Mark each such
   entry with `"source": "leaderboard_fallback"`, `"readiness": "cold"`, and
   `"why": "no direct history — closest active contributor by team leaderboard"`.
   Never leave `successors` as an empty list.
4. For direct-history successors, set `"source": "direct_history"` and assign
   the `readiness` bucket from the per-file recency score (ready/familiar/cold
   per the thresholds in sub-task 3).
5. Round all leaderboard scores to 2 decimal places; round per-file
   `recency_score` to 2 decimal places as well (consistent scale).
6. Create `backend/outputs/` with `os.makedirs(exist_ok=True)` before writing.
7. Write the report as pretty-printed JSON (`indent=2`).
8. Print the stdout summary (include the no-direct-successor count).

**Relevant Context:**
- Output directory: `backend/outputs/` — same as all other agents.
- All other agent outputs use `indent=2` pretty-printing.
- The `"agent"` envelope key matches the pattern in `contributor_report.json` and
  `complexity_report.json`.

---

## Sub-Task 5 — Wire up CLI and entry point

**Status:** `[ ] pending`

**Intent:**
Make `onboarding_agent.py` runnable as a standalone script with sensible defaults,
matching the CLI conventions of the other agents.

**Expected Outcomes:**
- Script is runnable as:
  ```bash
  python backend/agents/onboarding_agent.py
  # uses defaults: repo=sample-repos/steam-snap,
  #   risk_report=backend/outputs/risk_report.json,
  #   contributor_report=backend/outputs/contributor_report.json
  ```
- All three paths are overridable via positional or named CLI args.
- A `--help` message describes all arguments.

**Todo List:**
1. Use `argparse` with three optional arguments:
   - `--repo` (default: `sample-repos/steam-snap` relative to script location)
   - `--risk-report` (default: `backend/outputs/risk_report.json`)
   - `--contributor-report` (default: `backend/outputs/contributor_report.json`)
2. Wrap the pipeline (sub-tasks 1–4) in a `main()` function called from
   `if __name__ == "__main__":`.
3. No other changes to the script structure.

**Relevant Context:**
- Existing agents (`contributor_agent.py`, `complexity_agent.py`) use `sys.argv`
  directly; `onboarding_agent.py` is slightly more complex (3 inputs) so `argparse`
  is appropriate here.
- Default paths should be computed relative to the script's own `__file__` so the
  script works when called from any working directory.

---

## Shared Conventions (inherited from project)

- Lives in `backend/agents/`, writes to `backend/outputs/`.
- Python 3.9+; f-strings; `datetime.fromisoformat()`.
- No logging framework — `print()` for stdout, `sys.stderr.write()` for warnings.
- No new `requirements.txt` entries needed (`gitpython` already present).
- Never modifies `sample-repos/steam-snap`.
