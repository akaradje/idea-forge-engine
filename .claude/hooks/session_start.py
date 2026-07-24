"""SessionStart hook: inject current git state into context."""

import json
import subprocess


def run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ""


def main() -> None:
    status = run(["git", "status", "-sb"])
    log = run(["git", "log", "--oneline", "-5"])
    if not status and not log:
        return
    ctx = f"Git state at session start:\n{status}\n\nRecent commits:\n{log}"
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": ctx,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
