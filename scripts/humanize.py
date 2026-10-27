#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""humanize.py — AI 代码人性化/工程化改造分析

定位：只做"检测与清单"，不自动改写。改写着由 agent 按清单逐项执行，
每完成一类做一次真实 git 提交（改造真实发生 → 历史真实，不伪造）。

原则：
- 只改外观（命名、注释、风格、结构、错误处理），不改变任何功能/逻辑行为
- 不做：伪造提交日期/作者、假装长期开发、模仿特定真人笔迹、欺骗检测器

用法：
  python humanize.py <path>              # Markdown 改造清单
  python humanize.py <path> --format json
  python humanize.py <path> --min-score 3   # 只输出高价值项（可选）
"""

import argparse
import json
import os
import re
import sys

SRC_EXTS = {".cs", ".py", ".java", ".js", ".ts", ".c", ".cpp", ".h", ".hpp", ".go"}

# 启发式阈值集中在此，便于按语言/规模调整
LONG_FUNC_LINES = 80      # 函数体超过该行数判为超长
FUNC_SCAN_WINDOW = 4000  # 从函数起点向后扫描的字符窗口
STYLE_MIN_OCCUR = 5       # camel/snake 各达到该次数才判混用
MAGIC_MIN_OCCUR = 3       # 硬编码数字达到该数量才提示
CODE_TRUNCATE = 120       # 清单里代码行截断长度

# ---------- 启发式模式 ----------

# 1) 泛化标识符（AI 最爱；只匹配【声明的变量名】，排除类型关键字与属性访问）
GENERIC_NAMES = {
    "data", "datas", "result", "results", "temp", "tmp", "obj", "objects",
    "item", "items", "info", "infos", "payload", "params", "param", "args",
    "callback", "cb", "ret", "res", "flag", "output", "input", "body",
    "entry", "entries", "node", "nodes",
}
# 声明语句提取：var x = / let x = / const x =
VAR_DECL = re.compile(r"\b(?:var|let|const)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=")

# 2) 讲解型注释（解释"是什么"，AI 典型；真人注释多写"为什么/坑"）
TEACHING_COMMENT = re.compile(
    r"(?:#|//|///|/\*)\s*(?:这个|这是|用来|用于|表示|代表|定义|声明|"
    r"获取|设置|执行|创建|初始化|This (?:is|method|function|class|code)|"
    r"Creates?|Gets?|Sets?|Initializes?|Represents?|Defines?|Performs?|"
    r"used to|is used for)\b",
    re.IGNORECASE,
)

# 3) 调试残留
DEBUG_PATTERNS = [
    (r"\bprint\s*\(", "print("),
    (r"\bconsole\.(?:log|debug|warn)\s*\(", "console.*("),
    (r"\bDebug\.Write(?:Line|Error)?\s*\(", "Debug.Write*("),
    (r"\bSystem\.out\.(?:println|print)\s*\(", "System.out.*("),
    (r"\bprintf?\s*\(", "printf("),
    (r"\bprintln?\s*\(", "println("),
    (r"\bTODO\b", "TODO"),
    (r"\bFIXME\b", "FIXME"),
    (r"\bXXX\b", "XXX"),
    (r"\bHACK\b", "HACK"),
]

# 4) 吞异常（空 catch / except pass）
SWALLOWED_EXC = [
    re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}"),                       # catch (...) {}
    re.compile(r"catch\s*\([^)]*\)\s*\{\s*(?://[^\n]*\n\s*)*\}"),     # 只有注释的空 catch
    re.compile(r"except\s*[^:]*:\s*pass\b"),                          # except: pass
    re.compile(r"except\s*[^:]*:\s*(?:#\s*[^\n]*\n\s*)*pass\b"),
]

# 5) 命名法混用（同一文件内 camelCase 与 snake_case 同时出现，且都够多）
def _count_style(text, pattern):
    return len(re.findall(pattern, text))

CAMEL = re.compile(r"\b[a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*\b")
SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# 6) 超长函数（>80 行函数体）
FUNC_OPEN = re.compile(
    r"(?:public|private|protected|internal|static|async|virtual|override|"
    r"def|function|func|let\s+\w+\s*=\s*(?:async\s*)?\([^)]*\)\s*=>)\s+"
    r"[A-Za-z_][A-Za-z0-9_<>,\[\]\s]*\([^)]*\)\s*(?:\{|:|=>)",
    re.MULTILINE,
)

# 7) 硬编码数字/魔法数（AI 直接写死参数）
MAGIC_NUMBER = re.compile(r"[,=(]\s*(?:[1-9]\d{1,3}|0x[0-9a-fA-F]+)\s*[,);]")

# 8) 空函数体 / 空块
EMPTY_BLOCK = [
    re.compile(r"\{\s*\}\s*(?://[^\n]*)?\n", re.MULTILINE),  # {} 或带注释
    re.compile(r"pass\s*(?:#\s*[^\n]*)?\n"),
]


def read_text(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        # 单文件解码失败按空串跳过：不因为一个坏文件中断整批扫描
        return ""


def walk_sources(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "bin", "obj", "build", "node_modules", "dist", "out")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SRC_EXTS:
                out.append(os.path.join(dirpath, fn))
    return out


def analyze_file(path, text):
    """返回该文件的改造清单。"""
    issues = []
    lines = text.splitlines()

    # 泛化命名（只检测声明语句的变量名，如 var data = ...；不碰类型与属性访问）
    for i, line in enumerate(lines, 1):
        for m in VAR_DECL.finditer(line):
            name = m.group(1)
            if name in GENERIC_NAMES:
                issues.append({"type": "generic_name", "line": i, "code": line.strip()[:CODE_TRUNCATE],
                               "detail": "泛化命名 '%s'，建议改为业务含义名" % name})

    # 讲解型注释
    for i, line in enumerate(lines, 1):
        if TEACHING_COMMENT.search(line):
            issues.append({"type": "teaching_comment", "line": i, "code": line.strip()[:CODE_TRUNCATE],
                           "detail": "讲解型注释（解释'是什么'），建议改为'为什么/坑'或删掉"})

    # 调试残留
    for i, line in enumerate(lines, 1):
        for pat, label in DEBUG_PATTERNS:
            if re.search(pat, line):
                issues.append({"type": "debug_leftover", "line": i, "code": line.strip()[:CODE_TRUNCATE],
                               "detail": "调试残留 '%s'，发布前删除" % label})
                break

    # 吞异常
    for i, line in enumerate(lines, 1):
        for pat in SWALLOWED_EXC:
            if pat.search(line):
                issues.append({"type": "swallowed_exception", "line": i, "code": line.strip()[:CODE_TRUNCATE],
                               "detail": "空 catch/except（异常被吞），建议记录日志或处理"})
                break

    # 命名法混用
    camel = _count_style(text, CAMEL)
    snake = _count_style(text, SNAKE)
    if camel >= STYLE_MIN_OCCUR and snake >= STYLE_MIN_OCCUR:
        issues.append({"type": "style_inconsistency", "line": 0, "code": "",
                       "detail": "同一文件混用 camelCase(%d) 与 snake_case(%d)，建议统一（C#/Java 用 PascalCase/camelCase）"
                                 % (camel, snake)})

    # 超长函数
    for m in FUNC_OPEN.finditer(text):
        seg = text[m.start():m.start() + FUNC_SCAN_WINDOW]
        depth = 0
        body_lines = 0
        started = False
        for ch in seg:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
            elif ch == "\n" and started and depth >= 1:
                body_lines += 1
            if started and depth == 0 and body_lines > 0:
                break
        if body_lines > LONG_FUNC_LINES:
            ln = text[:m.start()].count("\n") + 1
            issues.append({"type": "long_function", "line": ln, "code": seg.split("(")[0][-60:].strip(),
                           "detail": "函数体 %d 行，建议拆分（>80 行是 AI 长函数典型）" % body_lines})

    # 魔法数（少量抽样，避免噪音）。先剔除 byte/数组字面量块——
    # AES 密钥、RGB 颜色、缓冲区初始化里的数字是合理常量，不算"硬编码"。
    body_no_arrays = re.sub(r"new\s+byte\s*\[\s*\]\s*\{[^}]*\}", "", text)
    body_no_arrays = re.sub(r"byte\s*\[\s*\]\s*\{[^}]*\}", "", body_no_arrays)
    magic = len(MAGIC_NUMBER.findall(body_no_arrays))
    if magic >= MAGIC_MIN_OCCUR:
        issues.append({"type": "magic_numbers", "line": 0, "code": "",
                       "detail": "%d 处硬编码数字，建议提取为具名常量（byte 数组/端口/超时等合理常量请人工排除）" % magic})

    # 空块
    for i, line in enumerate(lines, 1):
        for pat in EMPTY_BLOCK:
            if pat.search(line):
                issues.append({"type": "empty_block", "line": i, "code": line.strip()[:CODE_TRUNCATE],
                               "detail": "空块/空函数体，建议删除或补实现"})
                break

    return issues


def main():
    ap = argparse.ArgumentParser(description="AI 代码人性化改造分析（只检测不改写）")
    ap.add_argument("path", help="源码目录或单文件")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    args = ap.parse_args()

    p = os.path.abspath(args.path)
    if os.path.isdir(p):
        files = walk_sources(p)
    elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in SRC_EXTS:
        files = [p]
    else:
        print("error: 路径不是源码目录/文件:", p)
        return 1

    by_type = {}
    all_issues = []
    for f in files:
        text = read_text(f)
        if not text.strip():
            continue
        issues = analyze_file(f, text)
        for it in issues:
            it["file"] = f
            all_issues.append(it)
            by_type[it["type"]] = by_type.get(it["type"], 0) + 1

    if args.format == "json":
        print(json.dumps({
            "scanned_files": len(files),
            "summary": by_type,
            "issues": all_issues,
        }, ensure_ascii=False, indent=2))
        return 0

    # Markdown 清单
    print("# 人性化改造清单\n")
    print("扫描: %d 个源文件 | 发现 %d 处\n" % (len(files), len(all_issues)))
    if not all_issues:
        print("未发现明显 AI 痕迹。代码已较符合工程化习惯。")
        return 0
    print("## 汇总")
    for k, v in sorted(by_type.items(), key=lambda x: -x[1]):
        print("- **%s**: %d 处" % (k, v))
    print("\n## 明细（按文件）\n")
    cur = None
    for it in all_issues:
        if it["file"] != cur:
            cur = it["file"]
            print("### %s" % os.path.relpath(it["file"], p))
        ln = ("L%d" % it["line"]) if it["line"] else "-"
        print("- [%s] %s: %s" % (it["type"], ln, it["detail"]))
        if it["code"]:
            print("  `%s`" % it["code"])
    print("\n---\n改造步骤：按类型逐项修改（只改外观不改功能），每完成一类做一次真实 git 提交。")
    print("红线：不改任何逻辑/行为；不做假历史/假作者/模仿笔迹。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
