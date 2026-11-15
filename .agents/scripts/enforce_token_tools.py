"""PreToolUse hook to enforce ast-outline / bounded read_file on large files."""

import json
import os
import sys

SUPPORTED_EXTS = {
    ".cs", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh",
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx",
    ".java", ".kt", ".kts", ".scala", ".sc",
    ".go", ".rs", ".php", ".phtml", ".rb", ".rake", ".gemspec",
    ".ex", ".exs", ".lua", ".gd", ".swift",
    ".css", ".scss", ".sql", ".html", ".htm", ".vue",
    ".md", ".yaml", ".yml",
}

EXEMPT_BASENAMES = {"SKILL.md", "AGENTS.md", "GEMINI.md", "CLAUDE.md"}
EXEMPT_DIR_PARTS = {".agents", ".gemini", ".claude"}
MAX_UNBOUNDED_LINES = 200


def allow() -> None:
    print(json.dumps({"decision": "allow"}))


def deny(reason: str) -> None:
    print(json.dumps({"decision": "deny", "reason": reason}))


def is_exempt_path(path: str) -> bool:
    norm = os.path.normpath(path)
    parts = set(norm.split(os.sep))
    if parts & EXEMPT_DIR_PARTS:
        return True
    return os.path.basename(norm) in EXEMPT_BASENAMES


def count_lines(path: str, limit: int) -> int:
    count = 0
    with open(path, "rb") as f:
        for _ in f:
            count += 1
            if count > limit:
                return count
    return count


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
        return

    tool_call = payload.get("toolCall") or {}
    if tool_call.get("name") != "read_file":
        allow()
        return

    args = tool_call.get("args") or {}
    if args.get("StartLine") is not None or args.get("EndLine") is not None:
        allow()
        return

    path = args.get("Path") or ""
    if not path or is_exempt_path(path):
        allow()
        return

    _, ext = os.path.splitext(path)
    if ext.lower() not in SUPPORTED_EXTS:
        allow()
        return

    if not os.path.isfile(path):
        allow()
        return

    try:
        lines = count_lines(path, MAX_UNBOUNDED_LINES)
    except OSError:
        allow()
        return

    if lines <= MAX_UNBOUNDED_LINES:
        allow()
        return

    name = os.path.basename(path)
    deny(
        f"{name} exceeds {MAX_UNBOUNDED_LINES} lines. "
        f"Run `ast-outline \"{path}\"` or `ast-outline show \"{path}\" <Symbol>` first, "
        "or pass StartLine and EndLine to read_file."
    )


if __name__ == "__main__":
    main()
