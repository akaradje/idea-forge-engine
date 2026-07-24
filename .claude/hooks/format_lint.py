"""PostToolUse hook: auto-format + lint-gate for Python files.

Reads hook JSON from stdin. If the edited file is .py:
1. ruff format (auto-fix style)
2. ruff check --fix (auto-fix safe lint issues)
3. remaining errors are fed back to Claude via decision:block
"""

import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    fp = (payload.get("tool_input") or {}).get("file_path") or (
        payload.get("tool_response") or {}
    ).get("filePath", "")
    if not fp or not fp.endswith(".py") or not Path(fp).exists():
        return

    subprocess.run(["ruff", "format", "--quiet", fp], capture_output=True)
    subprocess.run(["ruff", "check", "--fix", "--quiet", fp], capture_output=True)
    check = subprocess.run(
        ["ruff", "check", "--output-format", "concise", fp],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0 and check.stdout.strip():
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "ruff found issues that need manual fixes:\n"
                    + check.stdout.strip()[:2000],
                }
            )
        )


if __name__ == "__main__":
    main()
