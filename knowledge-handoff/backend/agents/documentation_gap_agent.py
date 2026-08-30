#!/usr/bin/env python3
"""
Documentation Gap Agent
=======================
IBM TechXchange 2026 Hackathon — Knowledge Continuity Suite

Analyzes every relevant source file in a repository and assigns a
documentation score (0-100) based on six signals:

  1. Inline comments       — meaningful explanatory comments
  2. Docstrings            — Python/structured doc-blocks on functions/classes
  3. File-level header     — opening description at the top of the file
  4. External documentation— whether the file is referenced in README / docs/
  5. Documentation quality — distinguishes useful vs trivial comments
  6. Documentation completeness — penalises important undocumented constructs

Output: documentation_report.json compatible with contributor_report.json
        and complexity_report.json (joined on the "file" field).

Usage:
    python documentation_gap_agent.py --repo repos/steam-snap \
                                       --output documentation_report.json
"""

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# File extensions that are considered source/config files worth scoring.
SCORABLE_EXTENSIONS = {
    # Shell scripts
    ".sh", ".bash",
    # Python
    ".py",
    # YAML / TOML / JSON configs
    ".yaml", ".yml", ".toml", ".json",
    # Markdown / RST documentation (scored differently)
    ".md", ".rst", ".txt",
    # Desktop entries and similar
    ".desktop",
    # No extension — common for shell scripts in snap hooks/launchers
    "",
}

# Extensions to skip entirely (binary, generated, images, fonts, etc.)
SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".asc", ".gpg", ".key",
    ".pdf", ".zip", ".tar", ".gz", ".bz2",
    ".pyc", ".pyo", ".so", ".dll", ".a", ".lib",
    ".lock",
}

# Paths whose content is auto-generated or vendored and should not be
# treated as normal source files.
SKIP_PATH_FRAGMENTS = {
    "__pycache__", ".git/", "node_modules", ".tox", ".venv", "venv",
    "dist/", "build/",
}

# Files where documentation scoring is less meaningful (pure data / locks).
LOW_VALUE_FILES = {
    "requirements.txt", "constraints.txt",
    ".gitignore", ".gitattributes",
    ".pre-commit-config.yaml",
    "version",           # plain version number file
}

# Tiny wrapper / pass-through scripts: <= this many non-blank, non-comment lines
# are considered "trivially simple" for scoring adjustment purposes.
TRIVIAL_SCRIPT_MAX_CODE_LINES = 4

# Weight applied to each signal (must sum to 100)
W_INLINE      = 20   # meaningful inline comments
W_DOCSTRING   = 25   # docstrings / doc-blocks on functions & classes
W_FILE_HEADER = 15   # file-level header / module description
W_EXTERNAL    = 20   # referenced / explained in README or docs/
W_QUALITY     = 10   # quality of comments (penalise trivial)
W_COMPLETENESS = 10  # undocumented important constructs

assert W_INLINE + W_DOCSTRING + W_FILE_HEADER + W_EXTERNAL + W_QUALITY + W_COMPLETENESS == 100


# ---------------------------------------------------------------------------
# Helpers – file reading
# ---------------------------------------------------------------------------

def read_text(path: Path) -> Optional[str]:
    """Return file text, or None if unreadable / binary."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# External documentation index
# ---------------------------------------------------------------------------

def build_external_doc_index(repo_root: Path) -> dict[str, set[str]]:
    """
    Walk all Markdown/RST/text files in the repo and record which source-file
    names or path fragments they mention.  Returns a dict mapping each
    mentioning doc file path → set of mentioned file basenames/fragments.

    We also build an inverted index: source_basename → set of doc files.
    """
    mentioning: dict[str, set[str]] = {}   # doc_file -> {fragments mentioned}
    # Collect all doc-like files
    doc_extensions = {".md", ".rst", ".txt"}
    for fpath in repo_root.rglob("*"):
        if fpath.is_file() and fpath.suffix.lower() in doc_extensions:
            rel = str(fpath.relative_to(repo_root))
            text = read_text(fpath)
            if text:
                mentioning[rel] = set()
                # tokenise into words / path-like tokens
                tokens = re.findall(r"[\w.\-/\\]+", text)
                for tok in tokens:
                    mentioning[rel].add(tok.lower())
    return mentioning


def file_has_external_doc(
    rel_path: str,
    ext_index: dict[str, set[str]],
) -> bool:
    """
    Return True if any documentation file in the repo references this file's
    basename or a meaningful fragment of its relative path.
    """
    # Build candidate search tokens from the relative path
    parts = Path(rel_path).parts
    candidates = set()
    basename = Path(rel_path).name
    stem = Path(rel_path).stem
    candidates.add(basename.lower())
    candidates.add(stem.lower())
    # Also try parent/basename patterns like "src/steam-snap.sh"
    for i in range(len(parts)):
        fragment = "/".join(parts[i:]).lower()
        candidates.add(fragment)
        candidates.add("\\".join(parts[i:]).lower())

    for doc_rel, tokens in ext_index.items():
        # A doc file should not count as documenting itself
        if doc_rel == rel_path:
            continue
        for candidate in candidates:
            if candidate and len(candidate) > 3 and candidate in tokens:
                return True
    return False


# ---------------------------------------------------------------------------
# Shell script analyser
# ---------------------------------------------------------------------------

SHELL_COMMENT_RE = re.compile(r"^\s*#(.+)$", re.MULTILINE)
SHELL_SHEBANG_RE = re.compile(r"^#!")
# Patterns that indicate trivial / auto-generated comments
TRIVIAL_COMMENT_RE = re.compile(
    r"^\s*#\s*[-=*#]+\s*$"          # divider lines
    r"|^\s*#\s*TODO\s*$"             # bare TODO
    r"|^\s*#\s*$",                   # empty comment
    re.IGNORECASE,
)
# Patterns that strongly suggest meaningful explanatory content
MEANINGFUL_COMMENT_RE = re.compile(
    r"https?://"                      # URL reference
    r"|because|why|note:|workaround|reason|fix|issue|bug|caveat"
    r"|see\s|ref\.|reference|explanation|ensure|prevent|allow"
    r"|#\s*Set\b|#\s*This\b|#\s*Make\b|#\s*Force\b|#\s*Append\b|#\s*Export\b"
    r"|#\s*If\b|#\s*When\b|#\s*By\b|#\s*Check\b|#\s*Use\b|#\s*Handle\b"
    r"|#\s*Source\b|#\s*Configure\b|#\s*Copy\b|#\s*Clean\b|#\s*Define\b"
    r"|#\s*Download\b|#\s*Identify\b|#\s*Write\b|#\s*Skip\b|#\s*Create\b",
    re.IGNORECASE,
)

SHELL_FUNCTION_RE = re.compile(r"^\s*(?:function\s+)?(\w+)\s*\(\s*\)\s*\{", re.MULTILINE)


def analyse_shell(text: str, rel_path: str) -> dict:
    lines = text.splitlines()
    total_lines = len(lines)
    code_lines = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    comment_lines = [l for l in lines if SHELL_COMMENT_RE.match(l)
                     and not SHELL_SHEBANG_RE.match(l.strip())]

    # Distinguish meaningful from trivial comments
    meaningful = [c for c in comment_lines if not TRIVIAL_COMMENT_RE.match(c)]
    trivial    = [c for c in comment_lines if TRIVIAL_COMMENT_RE.match(c)]
    quality_comments = [c for c in meaningful if MEANINGFUL_COMMENT_RE.search(c)]

    comment_count = len(meaningful)

    # File-level header: a comment block in the first 10 lines
    first_10 = "\n".join(lines[:10])
    has_file_description = bool(
        len([l for l in lines[:10] if SHELL_COMMENT_RE.match(l)
             and not SHELL_SHEBANG_RE.match(l.strip())]) >= 2
    )

    # Functions defined (for completeness check)
    functions = SHELL_FUNCTION_RE.findall(text)
    # Each function near a comment?
    # Simple heuristic: count function definitions preceded by a comment
    documented_fns = 0
    for i, line in enumerate(lines):
        if re.match(r"\s*(?:function\s+)?(\w+)\s*\(\s*\)\s*\{", line):
            # Check lines above for a comment
            look_back = lines[max(0, i-3):i]
            if any(SHELL_COMMENT_RE.match(l) for l in look_back):
                documented_fns += 1

    docstring_count = documented_fns  # re-use field for documented functions

    return {
        "comment_count": comment_count,
        "docstring_count": docstring_count,
        "has_file_description": has_file_description,
        "total_lines": total_lines,
        "code_lines": len(code_lines),
        "functions_total": len(functions),
        "functions_documented": documented_fns,
        "quality_comment_count": len(quality_comments),
        "trivial_comment_count": len(trivial),
    }


# ---------------------------------------------------------------------------
# Python analyser
# ---------------------------------------------------------------------------

def analyse_python(text: str, rel_path: str) -> dict:
    lines = text.splitlines()
    total_lines = len(lines)

    # Count inline comments (not docstrings)
    comment_lines = [l for l in lines if re.match(r"\s*#(.+)$", l)
                     and not re.match(r"\s*#\s*$", l)]
    meaningful = [c for c in comment_lines if not TRIVIAL_COMMENT_RE.match(c)]
    quality_comments = [c for c in meaningful if MEANINGFUL_COMMENT_RE.search(c)]

    # Parse AST for docstrings
    docstring_count = 0
    functions_total = 0
    functions_documented = 0
    classes_total = 0
    classes_documented = 0
    module_docstring = False

    try:
        tree = ast.parse(text)
        # Module-level docstring
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)):
            module_docstring = True
            docstring_count += 1

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions_total += 1
                ds = ast.get_docstring(node)
                if ds:
                    functions_documented += 1
                    docstring_count += 1
            elif isinstance(node, ast.ClassDef):
                classes_total += 1
                ds = ast.get_docstring(node)
                if ds:
                    classes_documented += 1
                    docstring_count += 1
    except SyntaxError:
        pass

    # Also treat a leading comment block of >=2 lines (before first import/code)
    # as a file description — common Python convention for scripts without
    # formal module docstrings.
    leading_comment_lines = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not re.match(r"^#!", stripped):
            leading_comment_lines += 1
        else:
            break  # first non-comment, non-blank line reached

    has_file_description = module_docstring or (leading_comment_lines >= 2)

    return {
        "comment_count": len(meaningful),
        "docstring_count": docstring_count,
        "has_file_description": has_file_description,
        "total_lines": total_lines,
        "functions_total": functions_total,
        "functions_documented": functions_documented,
        "classes_total": classes_total,
        "classes_documented": classes_documented,
        "quality_comment_count": len(quality_comments),
        "trivial_comment_count": len(comment_lines) - len(meaningful),
    }


# ---------------------------------------------------------------------------
# YAML / config analyser
# ---------------------------------------------------------------------------

YAML_COMMENT_RE = re.compile(r"^\s*#(.+)$", re.MULTILINE)


def analyse_yaml(text: str, rel_path: str) -> dict:
    lines = text.splitlines()
    total_lines = len(lines)
    comment_lines = [l for l in lines if YAML_COMMENT_RE.match(l)
                     and not TRIVIAL_COMMENT_RE.match(l)]
    meaningful = [c for c in comment_lines if not TRIVIAL_COMMENT_RE.match(c)]
    quality_comments = [c for c in meaningful if MEANINGFUL_COMMENT_RE.search(c)]

    # File-level header: leading comment block
    leading_comments = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            leading_comments += 1
        elif stripped:
            break

    has_file_description = leading_comments >= 2

    # Rough measure of config complexity: count top-level keys
    top_level_keys = len(re.findall(r"^[a-zA-Z_][\w-]*\s*:", text, re.MULTILINE))

    return {
        "comment_count": len(meaningful),
        "docstring_count": 0,
        "has_file_description": has_file_description,
        "total_lines": total_lines,
        "top_level_keys": top_level_keys,
        "quality_comment_count": len(quality_comments),
        "trivial_comment_count": len(comment_lines) - len(meaningful),
    }


# ---------------------------------------------------------------------------
# Markdown / RST analyser  (documentation files themselves)
# ---------------------------------------------------------------------------

def analyse_markdown(text: str, rel_path: str) -> dict:
    """
    Documentation files are a different category.  A well-structured .md file
    in the docs/ directory is itself documentation; it gets credit purely for
    existing and being substantive.  We still check for minimal structure.
    """
    lines = text.splitlines()
    total_lines = len(lines)
    # Has a title?
    has_title = any(re.match(r"^#+ .+", l) for l in lines)
    # Non-trivial content (at least 10 content lines)
    content_lines = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    substantial = len(content_lines) >= 10

    return {
        "comment_count": 0,
        "docstring_count": 0,
        "has_file_description": has_title,
        "total_lines": total_lines,
        "content_lines": len(content_lines),
        "has_title": has_title,
        "is_substantial": substantial,
    }


# ---------------------------------------------------------------------------
# Desktop file analyser
# ---------------------------------------------------------------------------

def analyse_desktop(text: str, rel_path: str) -> dict:
    lines = text.splitlines()
    comment_lines = [l for l in lines if l.strip().startswith("#")]
    has_description = bool(re.search(r"^Comment=", text, re.MULTILINE))
    has_name = bool(re.search(r"^Name=", text, re.MULTILINE))
    return {
        "comment_count": len(comment_lines),
        "docstring_count": 0,
        "has_file_description": has_description and has_name,
        "total_lines": len(lines),
    }


# ---------------------------------------------------------------------------
# Score calculation
# ---------------------------------------------------------------------------

def score_file(
    rel_path: str,
    text: str,
    ext_index: dict[str, set[str]],
    repo_root: Path,
) -> dict:
    """
    Analyse a single file and return a scored result dict.
    """
    fpath = Path(rel_path)
    ext = fpath.suffix.lower()
    basename = fpath.name.lower()

    # ---- Dispatch to per-language analyser ---------------------------------
    is_doc_file = False
    if ext in (".md", ".rst",".txt"):
        analysis = analyse_markdown(text, rel_path)
        is_doc_file = True
    elif ext in (".yaml", ".yml", ".toml", ".json"):
        analysis = analyse_yaml(text, rel_path)
    elif ext == ".py":
        analysis = analyse_python(text, rel_path)
    elif ext == ".desktop":
        analysis = analyse_desktop(text, rel_path)
    else:
        # Shell scripts (no ext or .sh/.bash)
        analysis = analyse_shell(text, rel_path)

    # ---- External documentation signal ------------------------------------
    has_external_doc = file_has_external_doc(rel_path, ext_index)

    # ---- Scoring logic by file type ----------------------------------------

    # --- Documentation files (Markdown/RST) --------------------------------
    if is_doc_file:
        # Docs are themselves documentation; score reflects their own quality.
        score = _score_doc_file(analysis, rel_path)
        reason = _reason_doc_file(analysis, rel_path, score)
        return _make_result(rel_path, score, analysis, has_external_doc, reason)

    # --- Desktop entry files -----------------------------------------------
    if ext == ".desktop":
        score = _score_desktop(analysis, has_external_doc)
        reason = _reason_desktop(analysis, has_external_doc, score)
        return _make_result(rel_path, score, analysis, has_external_doc, reason)

    # --- YAML / config files -----------------------------------------------
    if ext in (".yaml", ".yml", ".toml"):
        score = _score_yaml(analysis, has_external_doc, rel_path, text)
        reason = _reason_yaml(analysis, has_external_doc, rel_path, score)
        return _make_result(rel_path, score, analysis, has_external_doc, reason)

    # --- Python files ------------------------------------------------------
    if ext == ".py":
        score = _score_python(analysis, has_external_doc)
        reason = _reason_python(analysis, has_external_doc, score)
        return _make_result(rel_path, score, analysis, has_external_doc, reason)

    # --- Shell scripts and misc (hooks, launchers) -------------------------
    # Trivially small scripts (pure wrappers, <=4 code lines) need minimal docs;
    # apply a floor so they aren't unfairly penalised as "critical gaps".
    code_lines = analysis.get("code_lines", analysis.get("total_lines", 0))
    is_trivial_wrapper = code_lines <= TRIVIAL_SCRIPT_MAX_CODE_LINES
    score = _score_shell(analysis, has_external_doc)
    if is_trivial_wrapper:
        # Floor: trivial wrappers with no comments can't score below 30
        # (there isn't much to document in 3 lines of exec-passthrough).
        score = max(30, score)
    reason = _reason_shell(analysis, has_external_doc, score, is_trivial_wrapper)
    return _make_result(rel_path, score, analysis, has_external_doc, reason)


def _make_result(rel_path, score, analysis, has_external_doc, reason):
    score = max(0, min(100, int(round(score))))
    return {
        "file": rel_path,
        "documentation_score": score,
        "metrics": {
            "comment_count":         analysis.get("comment_count", 0),
            "docstring_count":       analysis.get("docstring_count", 0),
            "has_file_description":  bool(analysis.get("has_file_description", False)),
            "has_external_documentation": has_external_doc,
        },
        "reason": reason,
    }


# ---- Python scoring -------------------------------------------------------

def _score_python(a, has_ext):
    fn_total = a.get("functions_total", 0)
    fn_doc   = a.get("functions_documented", 0)
    cls_total = a.get("classes_total", 0)
    cls_doc   = a.get("classes_documented", 0)
    comments  = a.get("comment_count", 0)
    quality   = a.get("quality_comment_count", 0)
    has_header = a.get("has_file_description", False)
    total_lines = max(1, a.get("total_lines", 1))

    # Signal 1: inline comments (W_INLINE=20)
    comment_density = comments / max(total_lines / 10, 1)  # per 10 lines
    s_inline = min(W_INLINE, int(comment_density * W_INLINE / 2))

    # Signal 2: docstrings (W_DOCSTRING=25)
    constructs = fn_total + cls_total
    if constructs == 0:
        s_docstring = W_DOCSTRING * 0.5  # no constructs to document
    else:
        doc_ratio = (fn_doc + cls_doc) / constructs
        s_docstring = doc_ratio * W_DOCSTRING

    # Signal 3: file header (W_FILE_HEADER=15)
    s_header = W_FILE_HEADER if has_header else 0

    # Signal 4: external docs (W_EXTERNAL=20)
    s_external = W_EXTERNAL if has_ext else 0

    # Signal 5: quality (W_QUALITY=10)
    if comments == 0:
        s_quality = 0
    else:
        q_ratio = quality / comments
        s_quality = q_ratio * W_QUALITY

    # Signal 6: completeness (W_COMPLETENESS=10)
    # Penalise undocumented functions/classes
    if constructs == 0:
        s_complete = W_COMPLETENESS * 0.6
    else:
        undoc_ratio = 1 - (fn_doc + cls_doc) / constructs
        s_complete = W_COMPLETENESS * (1 - undoc_ratio)

    return s_inline + s_docstring + s_header + s_external + s_quality + s_complete


def _reason_python(a, has_ext, score):
    parts = []
    has_header = a.get("has_file_description", False)
    fn_total = a.get("functions_total", 0)
    fn_doc   = a.get("functions_documented", 0)
    cls_total = a.get("classes_total", 0)
    cls_doc   = a.get("classes_documented", 0)
    constructs = fn_total + cls_total
    documented = fn_doc + cls_doc
    if not has_header:
        parts.append("no module-level docstring")
    else:
        parts.append("module docstring present")
    if constructs > 0:
        parts.append(f"{documented}/{constructs} functions/classes have docstrings")
    parts.append(f"{a.get('comment_count', 0)} meaningful inline comments")
    if has_ext:
        parts.append("referenced in external documentation")
    else:
        parts.append("not referenced in external documentation")
    return "; ".join(parts) + "."


# ---- Shell scoring --------------------------------------------------------

def _score_shell(a, has_ext):
    fn_total  = a.get("functions_total", 0)
    fn_doc    = a.get("functions_documented", 0)
    comments  = a.get("comment_count", 0)
    quality   = a.get("quality_comment_count", 0)
    has_header = a.get("has_file_description", False)
    total_lines = max(1, a.get("total_lines", 1))

    # Signal 1: inline comments
    comment_density = comments / max(total_lines / 10, 1)
    s_inline = min(W_INLINE, int(comment_density * W_INLINE / 2))

    # Signal 2: documented functions (repurposed W_DOCSTRING slot)
    if fn_total == 0:
        s_docstring = W_DOCSTRING * 0.5
    else:
        s_docstring = (fn_doc / fn_total) * W_DOCSTRING

    # Signal 3: file header
    s_header = W_FILE_HEADER if has_header else 0

    # Signal 4: external docs
    s_external = W_EXTERNAL if has_ext else 0

    # Signal 5: quality
    if comments == 0:
        s_quality = 0
    else:
        q_ratio = quality / comments
        s_quality = q_ratio * W_QUALITY

    # Signal 6: completeness — penalise undocumented functions
    if fn_total == 0:
        s_complete = W_COMPLETENESS * 0.6
    else:
        s_complete = (fn_doc / fn_total) * W_COMPLETENESS

    return s_inline + s_docstring + s_header + s_external + s_quality + s_complete


def _reason_shell(a, has_ext, score, is_trivial=False):
    parts = []
    if is_trivial:
        parts.append("trivial wrapper script (<=4 code lines; minimal documentation expected)")
    if a.get("has_file_description"):
        parts.append("has file-level comment header")
    else:
        parts.append("no file-level comment header")
    fn_total = a.get("functions_total", 0)
    fn_doc   = a.get("functions_documented", 0)
    if fn_total:
        parts.append(f"{fn_doc}/{fn_total} shell functions preceded by a comment")
    parts.append(f"{a.get('comment_count', 0)} meaningful inline comments")
    if has_ext:
        parts.append("referenced in external documentation")
    else:
        parts.append("not directly referenced in external docs")
    return "; ".join(parts) + "."


# ---- YAML scoring ---------------------------------------------------------

def _score_yaml(a, has_ext, rel_path, text):
    comments  = a.get("comment_count", 0)
    quality   = a.get("quality_comment_count", 0)
    has_header = a.get("has_file_description", False)
    top_level_keys = a.get("top_level_keys", 1)
    total_lines = max(1, a.get("total_lines", 1))

    # Large complex YAML files (like snapcraft.yaml) need more explanation.
    is_complex = total_lines > 100 or top_level_keys > 10

    # Signal 1
    comment_density = comments / max(total_lines / 10, 1)
    s_inline = min(W_INLINE, int(comment_density * W_INLINE / 2))

    # Signal 2 – no true docstrings; partial credit
    s_docstring = W_DOCSTRING * 0.4

    # Signal 3 – header
    s_header = W_FILE_HEADER if has_header else 0

    # Signal 4 – external docs
    s_external = W_EXTERNAL if has_ext else 0

    # Signal 5 – quality
    if comments == 0:
        s_quality = 0
    else:
        s_quality = (quality / comments) * W_QUALITY

    # Signal 6 – completeness: if the file is complex and has few comments,
    # penalise harder.
    if is_complex and comments < 5:
        s_complete = 0
    elif is_complex:
        s_complete = min(W_COMPLETENESS, int((comments / top_level_keys) * W_COMPLETENESS))
    else:
        s_complete = W_COMPLETENESS * 0.8

    return s_inline + s_docstring + s_header + s_external + s_quality + s_complete


def _reason_yaml(a, has_ext, rel_path, score):
    parts = []
    if a.get("has_file_description"):
        parts.append("has leading comment block")
    else:
        parts.append("no leading comment block")
    comments = a.get("comment_count", 0)
    parts.append(f"{comments} meaningful inline comments across {a.get('total_lines', 0)} lines")
    if has_ext:
        parts.append("referenced in external documentation")
    else:
        parts.append("not referenced in external documentation")
    return "; ".join(parts) + "."


# ---- Documentation file scoring ------------------------------------------

def _score_doc_file(a, rel_path):
    has_title   = a.get("has_title", a.get("has_file_description", False))
    substantial = a.get("is_substantial", False)
    content     = a.get("content_lines", 0)
    total       = max(1, a.get("total_lines", 1))

    # Docs are themselves documentation; high base credit
    base = 70 if has_title else 40
    if substantial:
        base += 15
    # Content ratio bonus
    content_ratio = content / total
    base += int(content_ratio * 15)
    return min(100, base)


def _reason_doc_file(a, rel_path, score):
    parts = []
    if a.get("has_title", a.get("has_file_description", False)):
        parts.append("has document title/header")
    else:
        parts.append("missing document title")
    lines = a.get("total_lines", 0)
    content = a.get("content_lines", 0)
    parts.append(f"{lines} total lines, {content} content lines")
    parts.append("this file is itself documentation; score reflects its own quality")
    return "; ".join(parts) + "."


# ---- Desktop file scoring ------------------------------------------------

def _score_desktop(a, has_ext):
    has_desc = a.get("has_file_description", False)
    # Desktop files: mostly data, minimal documentation opportunity.
    # Credit for having Name + Comment fields and external doc references.
    base = 30  # baseline for existing
    if has_desc:
        base += 25   # has Name and Comment fields
    if has_ext:
        base += 20
    if a.get("comment_count", 0) > 0:
        base += 10
    return min(100, base)


def _reason_desktop(a, has_ext, score):
    parts = []
    if a.get("has_file_description"):
        parts.append("has Name and Comment fields")
    else:
        parts.append("missing Comment field")
    if has_ext:
        parts.append("referenced in external documentation")
    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Repository walker
# ---------------------------------------------------------------------------

def should_skip(fpath: Path, repo_root: Path) -> bool:
    rel = str(fpath.relative_to(repo_root))
    # Skip if any path fragment matches skip list
    for frag in SKIP_PATH_FRAGMENTS:
        if frag in rel.replace("\\", "/"):
            return True
    # Skip binary / generated extensions
    if fpath.suffix.lower() in SKIP_EXTENSIONS:
        return True
    # Skip very low-value filenames
    if fpath.name.lower() in LOW_VALUE_FILES:
        return True
    return False


def walk_repo(repo_root: Path) -> list[Path]:
    """Return all files that should be analysed."""
    results = []
    for fpath in sorted(repo_root.rglob("*")):
        if not fpath.is_file():
            continue
        if should_skip(fpath, repo_root):
            continue
        ext = fpath.suffix.lower()
        name = fpath.name.lower()
        # Include files with scorable extensions, or no extension (hooks, scripts)
        if ext in SCORABLE_EXTENSIONS:
            results.append(fpath)
    return results


# ---------------------------------------------------------------------------
# Main agent entry point
# ---------------------------------------------------------------------------

def run_agent(repo_path: str, output_path: str):
    repo_root = Path(repo_path).resolve()
    if not repo_root.is_dir():
        print(f"ERROR: Repository path does not exist: {repo_root}", file=sys.stderr)
        sys.exit(1)

    print(f"[doc-agent] Repository: {repo_root}")
    print("[doc-agent] Building external documentation index...")
    ext_index = build_external_doc_index(repo_root)
    print(f"[doc-agent] Indexed {len(ext_index)} documentation files.")

    files_to_analyse = walk_repo(repo_root)
    print(f"[doc-agent] Files to analyse: {len(files_to_analyse)}")

    results = []
    for fpath in files_to_analyse:
        rel = str(fpath.relative_to(repo_root)).replace("\\", "/")
        text = read_text(fpath)
        if text is None:
            continue
        result = score_file(rel, text, ext_index, repo_root)
        results.append(result)
        print(f"  {result['documentation_score']:>3}  {rel}")

    # Sort by score ascending (worst gaps first for easy review)
    results.sort(key=lambda r: r["documentation_score"])

    report = {
        "agent": "documentation_gap_agent",
        "repo": str(repo_root),
        "file_count_analyzed": len(results),
        "files": results,
    }

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n[doc-agent] Report written to: {output_file}")
    print(f"[doc-agent] Files analysed: {len(results)}")

    # Print top-10 worst-documented files
    print("\n--- Top 10 documentation gaps ---")
    for r in results[:10]:
        print(f"  {r['documentation_score']:>3}  {r['file']}")
    print("\n--- Top 5 best-documented files ---")
    for r in results[-5:][::-1]:
        print(f"  {r['documentation_score']:>3}  {r['file']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Documentation Gap Agent — Knowledge Continuity Suite"
    )
    parser.add_argument(
        "--repo",
        default="repos/steam-snap",
        help="Path to the repository to analyse (default: repos/steam-snap)",
    )
    parser.add_argument(
        "--output",
        default="documentation_report.json",
        help="Output JSON report path (default: documentation_report.json)",
    )
    args = parser.parse_args()
    run_agent(args.repo, args.output)
