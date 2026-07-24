"""PreToolUse hook: block writing secrets into the repo.

Scans Write/Edit content for credential patterns and denies the tool call.
"""

import json
import re
import sys

PATTERNS = [
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "Anthropic API key"),
    (r"sk-[A-Za-z0-9]{40,}", "API secret key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub token"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "Private key"),
    (r"postgres(ql)?://\w+:[^@\s]{8,}@", "Database URL with password"),
]


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    ti = payload.get("tool_input") or {}
    text = " ".join(str(ti.get(k, "")) for k in ("content", "new_string", "command"))
    for pattern, label in PATTERNS:
        if re.search(pattern, text):
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                f"Blocked: {label} detected in content. "
                                "Secrets must live in .env (gitignored), "
                                "never in tracked files."
                            ),
                        }
                    }
                )
            )
            return


if __name__ == "__main__":
    main()
