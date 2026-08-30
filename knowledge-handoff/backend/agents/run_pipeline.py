"""
run_pipeline.py — Knowledge Continuity Suite
Reads the three real subagent outputs and feeds them into
orchestrator.compute_risk(), instead of using demo()'s standalone
reimplementation.

Usage:
    python run_pipeline.py \\
        outputs/contributor_report.json \\
        outputs/complexity_report.json \\
        outputs/documentation_report.json \\
        > outputs/risk_report.json

Full pipeline (run from backend/):

  Step 1 — collect raw data (Module 1)
    python agents/contributor_agent.py   <repo_path>
    python agents/complexity_agent.py    <repo_path>
    python agents/documentation_gap_agent.py <repo_path>

  Step 2 — merge + rank risk (Module 1)
    python agents/run_pipeline.py \\
        outputs/contributor_report.json \\
        outputs/complexity_report.json \\
        outputs/documentation_report.json \\
        > outputs/risk_report.json

  Step 3 — readiness gap analysis (Module 2)
    python agents/readiness_agent.py \\
        outputs/contributor_report.json \\
        outputs/risk_report.json
    Writes: outputs/readiness_report.json

  Step 4 — knowledge extraction drafts (Module 3)
    python agents/extraction_agent.py \\
        outputs/risk_report.json \\
        outputs/contributor_report.json \\
        [optional: <repo_path> if different from path stored in contributor_report.json]
    Writes: outputs/extraction_report.json
             outputs/extraction_drafts/<file>.md  (one per HIGH-risk file)
"""
import json
import sys
from orchestrator import compute_risk, generate_reason_llm


def load_report(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # Each report has shape {agent, repo, file_count_analyzed, files: [...]}
    return {entry["file"]: entry for entry in data["files"]}


def merge_reports(contributor_path: str, complexity_path: str, doc_path: str) -> list[dict]:
    contributor = load_report(contributor_path)
    complexity = load_report(complexity_path)
    doc = load_report(doc_path)

    # Union of all files seen across the three reports — a file might be
    # skipped by one agent (e.g. binary files skipped by complexity_agent)
    # but still present in the others.
    all_files = set(contributor) | set(complexity) | set(doc)

    merged = []
    skipped = []
    defaulted_doc = []
    for filepath in sorted(all_files):
        c = contributor.get(filepath)
        x = complexity.get(filepath)
        d = doc.get(filepath)

        # contributor + complexity are the foundation — both are required
        # to score risk at all (e.g. true binary files skipped by both).
        if c is None or x is None:
            skipped.append(filepath)
            continue

        # doc_score is best-effort: the Documentation Gap Agent
        # intentionally doesn't score some files (e.g. GPG keys, lock
        # files). Missing doc data genuinely means "no documentation
        # signal found" — default to 0 rather than dropping the file,
        # since these are often exactly the highest-risk files (e.g.
        # snap/keys/B05498B7.asc).
        if d is None:
            doc_score = 0
            defaulted_doc.append(filepath)
        else:
            doc_score = d.get("doc_score", d.get("documentation_score"))

        merged.append({
            "file": filepath,
            "author_count": c["author_count"],
            # translate field name: contributor_agent's
            # `days_since_last_touch` -> orchestrator's expected
            # `last_touch_days_ago`
            "last_touch_days_ago": c["days_since_last_touch"],
            "complexity_score": x["complexity_score"],
            "doc_score": doc_score,
        })

    if skipped:
        print(f"[warn] {len(skipped)} file(s) skipped, missing from contributor "
              f"or complexity report: {skipped[:5]}{'...' if len(skipped) > 5 else ''}",
              file=sys.stderr)
    if defaulted_doc:
        print(f"[info] {len(defaulted_doc)} file(s) had no documentation-agent "
              f"score, defaulted to doc_score=0: {defaulted_doc[:5]}"
              f"{'...' if len(defaulted_doc) > 5 else ''}",
              file=sys.stderr)

    return merged


def main():
    if len(sys.argv) != 4:
        print("Usage: python run_pipeline.py <contributor.json> <complexity.json> <doc.json>",
              file=sys.stderr)
        sys.exit(1)

    merged = merge_reports(sys.argv[1], sys.argv[2], sys.argv[3])
    ranked = compute_risk(merged)

    # Optional: use the real Bob-backed reason instead of the template one.
    # Uncomment if you want live Bob calls per HIGH-risk file (costs coins,
    # so only do this for the top N, not the whole repo):
    #
    # for r in ranked:
    #     if r["risk_level"] == "HIGH":
    #         r["why"] = generate_reason_llm(r)

    print(json.dumps(ranked, indent=2))


if __name__ == "__main__":
    main()