# Show HN draft (2026-07-24)

Submit at https://news.ycombinator.com/submit — GitHub URL in the **url** field, body in **text**.

## Title

Show HN: Idea Forge – demand-signal MCP server for Claude Code

## URL

https://github.com/akaradje/idea-forge-engine

## Text

I built an MCP server that lets a coding agent query live demand signals (Hacker News, Reddit) directly from the terminal — to find market gaps and check ideas against prior art.

Four tools:

- `fetch_hackernews` / `fetch_reddit` — pull Ask HN / Show HN / subreddit posts as structured docs

- `check_novelty` — search HN for prior art on an idea. It returns evidence (titles, points, comments, links), not a verdict. Your agent reads it and judges the overlap itself.

- `detect_gaps` — fetch posts and run an LLM pass to surface demand-supply gaps with cited quotes (needs an Anthropic key; everything else is keyless)

A design choice I feel strongly about: no "idea scores." I originally started building yet another AI idea validator, then dogfooded the tool on its own market — every idea-validator product I found on HN had 1–3 points, and the top related post was literally "Stop asking AI if your startup idea is good." So the tools return raw, verifiable signals and leave judgment to the agent (and you). The pivot decision is documented in the repo under research/.

Setup is one line: `claude mcp add idea-forge -- uv run --directory <path> idea-forge-mcp`

Python 3.12, MIT licensed. Would love feedback — especially on what other demand-signal sources would be worth adding.

https://github.com/akaradje/idea-forge-engine

## Notes on choices

- Title under 80 chars, names the category (MCP server for Claude Code) — targets the agent-tooling audience that showed 100+ pt engagement in the dogfood research, not "startup idea" people.
- The self-deprecating pivot story preempts the "another AI validator" dismissal and is verifiable via `research/2026-07-24-dogfood-findings.md` in the public repo.
