# Knowledge Continuity Suite — Agents Plan

## Overview

Two independent Python scripts (`agents/contributor_agent.py` and `agents/complexity_agent.py`) analyse a target git repository and emit JSON reports to `outputs/`. They share no code and have no runtime dependency on each other — either can be run standalone.

A minimal `requirements.txt` must be created at the project root before the agents are written.

---

## Sub-Task 1 — Create requirements.txt

**Status:** `[x] done`

**Intent:**
Both agents need third-party libraries. Declaring them in a single `requirements.txt` at the project root makes the environment reproducible and keeps each agent script free of inline install logic.

**Expected Outcomes:**
- `requirements.txt` exists at the project root.
- Running `pip install -r requirements.txt` installs all dependencies needed by both agents without error.

**Todo List:**
1. Create `requirements.txt` with the following entries:
   - `lizard` — cyclomatic complexity analysis (used by `complexity_agent.py`)
   - `gitpython` — programmatic git log access (used by `contributor_agent.py`)
2. Pin no specific versions initially; allow pip to resolve latest compatible.

**Relevant Context:**
- `lizard` is a PyPI library that supports `.py`, `.js`, `.ts`, `.go`, `.c`, `.cpp`, `.java`, `.sh`, `.rb`, `.rs` and returns per-function cyclomatic complexity plus NLOC.
- `gitpython` (package `git`) provides `Repo`, `Commit`, and `Blob` objects for iterating git history without shelling out.

---

## Sub-Task 2 — contributor_agent.py

**Status:** `[x] done`

**Intent:**
Walk every tracked file in a target repo, query `git log` per file to extract author identities and timestamps, then emit a sorted JSON report that surfaces knowledge silos (files touched by few people) and stale files (long since last modified).

**Expected Outcomes:**
- `agents/contributor_agent.py` exists and is runnable as `python agents/contributor_agent.py <repo_path>`.
- Output JSON is written to `outputs/contributor_report.json`.
- Files are sorted: fewest distinct authors first; ties broken by most days since last touch (descending).
- Output schema:
  ```
  {
    "agent": "contributor_agent",
    "repo": "<absolute path>",
    "file_count_analyzed": <int>,
    "files": [
      {
        "file": "<repo-relative path>",
        "author_count": <int>,
        "authors": ["Name <email>", ...],
        "commit_count": <int>,
        "last_touch_date": "YYYY-MM-DD",
        "days_since_last_touch": <int>
      }
    ]
  }
  ```

**Todo List:**
1. Accept a single CLI argument: `repo_path` (path to a local git repository). Default to current working directory if omitted.
2. Open the repo with `git.Repo(repo_path)`.
3. Collect the list of all tracked files from the HEAD tree by traversing `repo.head.commit.tree` recursively (use the `blobs` and `trees` iterators). Record each file's repo-relative path.
4. For each file, call `repo.git.log("--follow", "--format=%ae%x09%aI", "--", file_path)` to obtain one `email<TAB>ISO-timestamp` line per commit.
   - Parse distinct author emails/names for `author_count` and `authors`.
   - Count total lines for `commit_count`.
   - Parse the first (most recent) timestamp line for `last_touch_date` and compute `days_since_last_touch` relative to today's UTC date.
5. Sort the resulting list: primary key `author_count` ascending; secondary key `days_since_last_touch` descending.
6. Build the output dict and write it as pretty-printed JSON to `outputs/contributor_report.json`. Create `outputs/` if it does not exist.
7. Print a one-line summary to stdout: `Analyzed N files in <repo>`.

**Relevant Context:**
- Use `--format=%aN <%ae>%x09%aI` to capture both display name and email in a single log call, keeping the data per-line parseable.
- `%aI` is ISO 8601 strict format; use `datetime.fromisoformat()` to parse it (Python 3.7+).
- `--follow` handles renames so file history is not truncated at rename boundaries.
- The `outputs/` directory is an empty placeholder per AGENTS.md; create it with `os.makedirs(exist_ok=True)`.

---

## Sub-Task 3 — complexity_agent.py

**Status:** `[x] done`

**Intent:**
Score every file in a target repo for complexity. Code files are analysed with `lizard` (cyclomatic complexity + NLOC); non-code text files fall back to raw line count; binary files are skipped entirely. All scores are normalised to a 0–100 scale so consumers can compare files uniformly regardless of language or type.

**Expected Outcomes:**
- `agents/complexity_agent.py` exists and is runnable as `python agents/complexity_agent.py <repo_path>`.
- Output JSON is written to `outputs/complexity_report.json`.
- Binary files (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.ico`, `.svg`, `.woff`, `.woff2`, `.ttf`, `.eot`, `.otf`, `.zip`, `.tar`, `.gz`, `.bz2`, `.xz`, `.pdf`, `.exe`, `.so`, `.dylib`, `.dll`, `.class`, `.jar`, `.pyc`) are not present in the output at all.
- Output schema:
  ```
  {
    "agent": "complexity_agent",
    "repo": "<absolute path>",
    "file_count_analyzed": <int>,
    "files": [
      {
        "file": "<repo-relative path>",
        "complexity_score": <float 0-100>,
        "metrics": {
          "nloc": <int>,
          "function_count": <int>,
          "avg_cyclomatic_complexity": <float | null>,
          "max_cyclomatic_complexity": <int | null>
        }
      }
    ]
  }
  ```
- Files are sorted by `complexity_score` descending (highest complexity first).

**Todo List:**
1. Accept a single CLI argument: `repo_path`. Default to current working directory if omitted.
2. Define two sets of constants:
   - `CODE_EXTENSIONS`: `{".py", ".js", ".ts", ".go", ".c", ".cpp", ".java", ".sh", ".rb", ".rs"}`
   - `BINARY_EXTENSIONS`: the set listed in Expected Outcomes above.
3. Collect all tracked files from the HEAD tree (same traversal as contributor_agent).
4. For each file:
   a. Skip immediately if the file extension is in `BINARY_EXTENSIONS`.
   b. Resolve the absolute path by joining `repo_path` with the repo-relative path.
   c. If extension is in `CODE_EXTENSIONS`: run `lizard.analyze_file(abs_path)`. Extract:
      - `nloc` = `file_info.nloc`
      - `function_count` = `len(file_info.function_list)`
      - `avg_cyclomatic_complexity` = mean of `[fn.cyclomatic_complexity for fn in file_info.function_list]`, or `null` if no functions.
      - `max_cyclomatic_complexity` = max of the same list, or `null` if no functions.
      - Raw score for normalisation: `nloc + (avg_cyclomatic_complexity or 0) * 10`
   d. Otherwise (text file, non-code): count lines by reading the file; set `nloc = line_count`, all cyclomatic fields `null`. Raw score = `nloc`.
   e. On any `OSError` or `UnicodeDecodeError` when reading, skip the file and log a warning to stderr.
5. Normalise all raw scores to 0–100:
   - `complexity_score = (raw / max_raw) * 100` where `max_raw` is the maximum raw score across all analysed files.
   - If all files have raw score 0, set all `complexity_score` to 0.
   - Round to two decimal places.
6. Sort the output list by `complexity_score` descending.
7. Write pretty-printed JSON to `outputs/complexity_report.json`. Create `outputs/` if needed.
8. Print a one-line summary to stdout: `Analyzed N files in <repo> (M skipped as binary)`.

**Relevant Context:**
- `lizard.analyze_file(path)` returns a `FileInformation` object; it does not raise on empty files, but `function_list` will be empty.
- The normalisation formula is intentionally simple (linear min-max against the corpus max). This keeps the score interpretable without external dependencies.
- `CODE_EXTENSIONS` must include `.sh` even though lizard treats shell scripts as line-count only internally — lizard still returns a valid `FileInformation` with `nloc` populated.
- Both agents traverse the git tree rather than the filesystem to avoid analysing untracked or gitignored files (e.g. `node_modules/`, `__pycache__/`).

---

## Shared Conventions

- Both agents live in `agents/` and write to `outputs/`.
- Both accept `repo_path` as a positional CLI arg (default: cwd).
- Both produce a single `.json` file named after the agent (`contributor_report.json`, `complexity_report.json`).
- No shared utility module is needed; duplication (git tree traversal) is acceptable given the two-file scope.
- Python 3.9+ is assumed (f-strings, `datetime.fromisoformat`, `os.makedirs(exist_ok=True)`).
- No logging framework — `print()` for stdout summary, `sys.stderr.write()` for warnings.
