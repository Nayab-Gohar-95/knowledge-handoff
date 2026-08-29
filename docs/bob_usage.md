### Session 1 — Orchestrator core logic
**Date:** Aug 28, 2026

**Interface:** Bob IDE (chat panel)

**Task:** Generate `backend/agents/orchestrator.py` — merges Contributor,
Complexity, and Documentation Gap agent outputs into a ranked bus-factor
risk report.

**Prompt used:** [full spec — merge logic, risk thresholds, template +
Bob-Shell-backed reason generation, standalone git-repo test harness]

**What Bob produced:**
- `compute_risk(files)` — main entry point; computes `median(complexity_score)`
  dynamically rather than hardcoding a threshold, classifies each file via
  `_classify()` (HIGH/MEDIUM/LOW per spec), sorts HIGH→MEDIUM→LOW tie-broken
  by ascending doc_score
- `_generate_reason(f)` — template-based reason, pure function; handles edge
  cases Bob added on its own initiative (doc_score == 0 → "zero comments",
  singular vs. plural "contributor"/"day")
- `generate_reason_llm(f)` — calls `bob -p` via `subprocess.run` with a
  15-second timeout; catches `FileNotFoundError`, `TimeoutExpired`, and other
  `OSError`, falling back silently to the template reason. This is Bob
  embedded in the running solution, not just used to build it.
- `demo(repo_path)` — standalone test harness: walks tracked files via
  `git ls-files`, pulls author/date history via `git log --follow`, scores
  complexity via LOC + branch-keyword count, scores docs via `ast.walk`
  (Python) or comment-density (JS/TS), prints a ranked RISK/FILE/WHY table

**Manual corrections needed:** None from me — Bob self-corrected twice
during the session: removed an unused parameter it introduced, and fixed
a bug in its own test assertion (the code was actually right; the test's
string-match check was wrong) after a smoke test failure.
