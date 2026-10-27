#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_repo.py - Extract objective signals from a source tree / git repo
to help judge whether code was likely written by an AI model.

Usage:
    python analyze_repo.py <path-to-source-dir-or-git-repo>

The script ONLY extracts objective, reproducible metrics. It does NOT
conclude "AI or human" - the agent combines these metrics with the
subjective signal checklist (references/signals.md) to reach a verdict.

Exit code: 0 on success.
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter

# Source file extensions we analyze (lowercase, with dot)
SRC_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp",
    ".hpp", ".cc", ".cs", ".go", ".rs", ".php", ".rb", ".kt", ".sh",
    ".sql", ".html", ".css", ".vue", ".m", ".mm", ".swift", ".scala",
}

# Directories always skipped
SKIP_DIRS = {
    ".git", "node_modules", "build", "bin", "dist", "out", "target",
    ".venv", "venv", "__pycache__", ".idea", ".vscode", ".gradle",
    "Pods", "DerivedData", ".cache", "vendor",
}

# 启发式截断/超时阈值：单独提出来方便按环境调整，不散落内联
MAX_CANDIDATES = 20          # 未用 import / 重赋值候选最多返回多少条
GIT_TIMEOUT_QUICK = 5       # rev-parse 探测仓库，秒
GIT_TIMEOUT_HISTORY = 10    # 历史统计命令，秒

# Generic / low-information identifier tokens (AI-style or lazy naming)
GENERIC_TOKENS = [
    r"\bdata\b", r"\bitem\b", r"\bresult\b", r"\btemp\b", r"\btmp\b",
    r"\bobj\b", r"\btest123\b", r"\bvalue\b", r"\bparam\b", r"\bargs\b",
    r"\bfoo\b", r"\bbar\b", r"\bthing\b", r"\bmisc\b",
]

# Debug leftovers
DEBUG_PATTERNS = [
    (r"console\.log\s*\(", "console.log"),
    (r"\bprint\s*\(", "print("),
    (r"Debug\.Write(Line)?\s*\(", "Debug.Write*"),
    (r"printf\s*\(", "printf("),
    (r"\bprintln\s*\(", "println("),
    (r"TODO", "TODO"),
    (r"FIXME", "FIXME"),
    (r"\bXXX\b", "XXX"),
    (r"\bHACK\b", "HACK"),
]

# Dead-code related comment markers
DEAD_CODE_MARKERS = [
    r"dead code", r"unused", r"not used", r"unreachable",
    r"never used", r"commented out", r"todo: remove", r"xxx: remove",
]

# Placeholder return values (unimplemented / stub behavior)
PLACEHOLDER_RETURNS = [
    r"return null\s*;", r"return 0\s*;", r"return None\b", r"return false\s*;",
    r"return \"\"\s*;", r"return -1\s*;", r"return \[\]\s*;",
]

# Import / using statement patterns by language
IMPORT_PATTERNS = {
    "py": (r"^\s*(?:import|from)\s+([\w.]+)", "py"),
    "java": (r"^\s*import\s+([\w.]+)\s*;", "java"),
    "cs": (r"^\s*using\s+([\w.]+)\s*;", "cs"),
    "js": (r"^\s*(?:import\s+.*?from\s+['\"])([\w\-./@]+)(?:['\"])", "js"),
    "ts": (r"^\s*(?:import\s+.*?from\s+['\"])([\w\-./@]+)(?:['\"])", "ts"),
}


def is_language_for_imports(ext):
    return ext in IMPORT_PATTERNS


def find_unused_imports(ext, text):
    """Heuristic: imported module/package names that appear nowhere else in the file.
    Only for py/java/cs/js/ts. Returns list of candidate names (top 20)."""
    if not is_language_for_imports(ext):
        return []
    pattern, _ = IMPORT_PATTERNS[ext]
    candidates = set()
    for m in re.finditer(pattern, text, flags=re.MULTILINE):
        name = m.group(1)
        base = name.split(".")[-1]
        if base and len(base) >= 2:
            candidates.add(base)
    if not candidates:
        return []
    unused = []
    for c in candidates:
        # count occurrences of the bare name outside its own import line
        body = re.sub(pattern, "", text, flags=re.MULTILINE)
        if len(re.findall(r"\b" + re.escape(c) + r"\b", body)) == 0:
            unused.append(c)
    return unused[:MAX_CANDIDATES]


def find_reassigned(ext, text):
    """Heuristic: same identifier assigned twice in immediate succession
    (x = ...; x = ...) - common leftover pattern."""
    hits = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*[^;{}]{0,60};[^;{}]{0,20}\b\1\s*=", text):
        hits.append(m.group(1))
    # dedupe, cap
    seen = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return seen[:MAX_CANDIDATES]


def find_stubbed_functions(ext, text):
    """Heuristic: function bodies that are empty/placeholder (pass / return null / return 0).
    Language-agnostic regex; intentionally conservative."""
    stubs = 0
    # python: def x(...):\n    pass
    stubs += len(re.findall(r"def\s+\w+[^:]*:\s*\n\s*pass\b", text))
    # java/c#/js/ts/c: { return null; }  /  { return 0; }  /  { }  one-liner bodies
    stubs += len(re.findall(r"\{\s*return\s+(?:null|0|false)\s*;\s*\}", text))
    stubs += len(re.findall(r"\{\s*throw\s+new\s+\w+Exception\([^)]*\)\s*;\s*\}", text))
    return stubs


def count_placeholder_returns(text):
    return sum(len(re.findall(p, text)) for p in PLACEHOLDER_RETURNS)


def count_dead_code_comments(text):
    return sum(len(re.findall(p, text, flags=re.IGNORECASE)) for p in DEAD_CODE_MARKERS)

# Comment markers by language family
LINE_COMMENTS = {"#", "//", "--"}
BLOCK_START = {"/*", "<!--"}
BLOCK_END = {"*/", "-->"}


def walk_sources(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in SRC_EXTS:
                files.append(os.path.join(dirpath, fn))
    return files


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="ignore")


def count_comments(lines):
    """Return (total_lines, comment_lines, inline_comment_lines)."""
    total = 0
    comment_lines = 0
    inline = 0
    in_block = False
    for raw in lines:
        line = raw.rstrip("\n\r")
        total += 1
        stripped = line.strip()
        if in_block:
            comment_lines += 1
            if any(stripped.startswith(e) or e in stripped for e in BLOCK_END):
                in_block = False
            continue
        if any(stripped.startswith(s) for s in LINE_COMMENTS):
            comment_lines += 1
            continue
        if any(stripped.startswith(s) for s in BLOCK_START):
            comment_lines += 1
            if not any(e in stripped for e in BLOCK_END):
                in_block = True
            continue
        # inline comment: code then comment marker
        if "#" in line or "//" in line:
            inline += 1
    return total, comment_lines, inline


def count_generic_tokens(text):
    hits = 0
    for pat in GENERIC_TOKENS:
        hits += len(re.findall(pat, text, flags=re.IGNORECASE))
    return hits


def count_debug(text):
    counts = {}
    for pat, label in DEBUG_PATTERNS:
        n = len(re.findall(pat, text))
        if n:
            counts[label] = n
    return counts


def git_stats(root):
    """Collect git history metrics. Returns dict or None if not a git repo."""
    git_dir = os.path.join(root, ".git")
    if not os.path.isdir(git_dir):
        # maybe root is inside a repo
        try:
            subprocess.run(
                ["git", "-C", root, "rev-parse", "--git-dir"],
                capture_output=True, timeout=GIT_TIMEOUT_QUICK, check=True,
            )
        except Exception:
            return None
    out = {}
    try:
        # commit count
        r = subprocess.run(["git", "-C", root, "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT_HISTORY)
        out["commit_count"] = int(r.stdout.strip() or 0) if r.returncode == 0 else 0
        # authors
        r = subprocess.run(["git", "-C", root, "shortlog", "-sn", "HEAD"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT_HISTORY)
        authors = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
        out["author_count"] = len(authors)
        # commit messages
        r = subprocess.run(["git", "-C", root, "log", "--format=%s"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT_HISTORY)
        msgs = [l for l in r.stdout.splitlines() if l.strip()]
        if msgs:
            prefixed = sum(1 for m in msgs if re.match(r"^(feat|fix|refactor|docs|chore|style|perf|test|build|ci|revert|wip)[!: ]", m, re.I))
            out["conventional_prefix_ratio"] = round(prefixed / len(msgs), 2)
            lens = [len(m) for m in msgs]
            out["avg_msg_len"] = round(sum(lens) / len(lens), 1)
        # time span
        r = subprocess.run(["git", "-C", root, "log", "--format=%at"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT_HISTORY)
        stamps = [int(x) for x in r.stdout.split() if x.strip()]
        if len(stamps) >= 2:
            span_days = (max(stamps) - min(stamps)) / 86400.0
            out["span_days"] = round(span_days, 1)
            out["single_burst_ratio"] = round(
                sum(1 for s in stamps if max(stamps) - s < 3600) / len(stamps), 2
            )
    except Exception:
        # git 不可用或非仓库时历史指标直接为 None，属正常缺失，不中断整体扫描
        return None
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: python analyze_repo.py <path>")
        return 1
    root = os.path.abspath(sys.argv[1])
    if not os.path.isdir(root):
        print("error: not a directory:", root)
        return 1

    files = walk_sources(root)
    total_lines = 0
    comment_lines = 0
    inline_lines = 0
    generic_tokens = 0
    debug_hits = Counter()
    by_ext = Counter()
    unused_imports = []
    reassigned = []
    stubbed = 0
    placeholder_returns = 0
    dead_code_comments = 0

    for f in files:
        try:
            text = read_text(f)
        except Exception:
            continue
        ext = os.path.splitext(f)[1].lower()
        lines = text.splitlines()
        t, c, i = count_comments(lines)
        total_lines += t
        comment_lines += c
        inline_lines += i
        generic_tokens += count_generic_tokens(text)
        for pat, label in DEBUG_PATTERNS:
            n = len(re.findall(pat, text))
            if n:
                debug_hits[label] += n
        unused_imports.extend(find_unused_imports(ext, text))
        reassigned.extend(find_reassigned(ext, text))
        stubbed += find_stubbed_functions(ext, text)
        placeholder_returns += count_placeholder_returns(text)
        dead_code_comments += count_dead_code_comments(text)
        by_ext[ext] += 1

    report = {
        "root": root,
        "source_files": len(files),
        "ext_distribution": dict(by_ext.most_common(8)),
        "total_lines": total_lines,
        "comment_lines": comment_lines,
        "comment_ratio": round(comment_lines / total_lines, 3) if total_lines else 0,
        "inline_comment_ratio": round(inline_lines / total_lines, 3) if total_lines else 0,
        "generic_identifiers_per_1k_lines": round(generic_tokens * 1000 / total_lines, 1) if total_lines else 0,
        "debug_leftovers": dict(debug_hits),
        "has_todo_fixme": any(k in ("TODO", "FIXME") for k in debug_hits),
        "dead_code": {
            "unused_import_candidates": unused_imports[:MAX_CANDIDATES],
            "reassigned_identifiers": reassigned[:20],
            "stubbed_functions": stubbed,
            "placeholder_returns": placeholder_returns,
            "dead_code_comments": dead_code_comments,
        },
    }
    report["git"] = git_stats(root)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
