"""complexity_agent.py — score every tracked file in a git repo for complexity.

Usage:
    python agents/complexity_agent.py [repo_path]

repo_path defaults to the current working directory if omitted.
"""

import json
import os
import sys
from statistics import mean

import git
import lizard

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".tar", ".gz", ".bz2", ".xz",
    ".pdf", ".exe", ".so", ".dylib", ".dll",
    ".class", ".jar", ".pyc",
}

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".go", ".c", ".cpp",
    ".java", ".sh", ".rb", ".rs",
}


def collect_tracked_files(repo):
    """Return a list of repo-relative paths for every blob in HEAD."""
    paths = []

    def walk_tree(tree):
        for blob in tree.blobs:
            paths.append(blob.path)
        for subtree in tree.trees:
            walk_tree(subtree)

    walk_tree(repo.head.commit.tree)
    return paths


def analyze_file(abs_path, ext):
    """Return (nloc, function_count, avg_cc, max_cc, raw_score) or None on error."""
    if ext in CODE_EXTENSIONS:
        try:
            file_info = lizard.analyze_file(abs_path)
        except OSError as exc:
            sys.stderr.write(f"WARNING: skipping {abs_path}: {exc}\n")
            return None

        nloc = file_info.nloc
        fn_list = file_info.function_list
        function_count = len(fn_list)

        if fn_list:
            cc_values = [fn.cyclomatic_complexity for fn in fn_list]
            avg_cc = mean(cc_values)
            max_cc = max(cc_values)
        else:
            avg_cc = None
            max_cc = None

        raw = nloc + (avg_cc or 0) * 10
        return nloc, function_count, avg_cc, max_cc, raw

    else:
        # Non-code, non-binary: count lines
        try:
            with open(abs_path, encoding="utf-8", errors="strict") as fh:
                line_count = sum(1 for _ in fh)
        except (OSError, UnicodeDecodeError) as exc:
            sys.stderr.write(f"WARNING: skipping {abs_path}: {exc}\n")
            return None

        return line_count, 0, None, None, float(line_count)


def main():
    repo_path = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    repo_path = os.path.abspath(repo_path)

    repo = git.Repo(repo_path)
    tracked = collect_tracked_files(repo)

    records = []   # (rel_path, nloc, function_count, avg_cc, max_cc, raw)
    binary_count = 0

    for rel_path in tracked:
        ext = os.path.splitext(rel_path)[1].lower()

        if ext in BINARY_EXTENSIONS:
            binary_count += 1
            continue

        abs_path = os.path.join(repo_path, rel_path)
        result = analyze_file(abs_path, ext)
        if result is None:
            continue

        nloc, function_count, avg_cc, max_cc, raw = result
        records.append((rel_path, nloc, function_count, avg_cc, max_cc, raw))

    max_raw = max((r[5] for r in records), default=0)

    files_out = []
    for rel_path, nloc, function_count, avg_cc, max_cc, raw in records:
        score = round((raw / max_raw) * 100, 2) if max_raw > 0 else 0.0
        files_out.append({
            "file": rel_path,
            "complexity_score": score,
            "metrics": {
                "nloc": nloc,
                "function_count": function_count,
                "avg_cyclomatic_complexity": avg_cc,
                "max_cyclomatic_complexity": max_cc,
            },
        })

    files_out.sort(key=lambda x: x["complexity_score"], reverse=True)

    output = {
        "agent": "complexity_agent",
        "repo": repo_path,
        "file_count_analyzed": len(files_out),
        "files": files_out,
    }

    os.makedirs("outputs", exist_ok=True)
    with open("outputs/complexity_report.json", "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)

    print(f"Analyzed {len(files_out)} files in {repo_path} ({binary_count} skipped as binary)")


if __name__ == "__main__":
    main()
