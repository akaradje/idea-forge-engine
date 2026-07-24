# Idea Forge

**A demand-signal terminal for Claude Code.**

Idea Forge is an MCP server that gives your coding agent direct access to live demand signals — what people are asking for, complaining about, and building — so it can find market gaps and check ideas against prior art without leaving your terminal.

Instead of a dashboard you read, it's a data layer your agent queries.

## Tools

| Tool | Needs API key? | What it does |
|------|----------------|--------------|
| `fetch_hackernews` | No | Pull Ask HN / Show HN posts (Algolia HN Search), optionally filtered by query |
| `fetch_reddit` | Reddit creds | Pull posts from subreddits like r/SomebodyMakeThis |
| `check_novelty` | No | Search HN for prior art on an idea — returns evidence, not verdicts; your agent judges similarity |
| `detect_gaps` | `ANTHROPIC_API_KEY` | Fetch posts and run LLM analysis to surface demand-supply gaps with cited evidence |

Design principle: **evidence over opinion.** Tools return raw, verifiable signals (posts, points, comments, quotes) and leave the judgment to the agent — no black-box "idea scores."

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo> && cd idea-forge-engine
uv sync
```

Register with Claude Code:

```bash
claude mcp add idea-forge -- uv run --directory /path/to/idea-forge-engine idea-forge-mcp
```

Then ask Claude Code things like:

- *"What are people asking for on HN this week that doesn't exist yet?"* → `detect_gaps` (or `fetch_hackernews` + your agent's own analysis, no key needed)
- *"Has anyone built a CI write-budget gate before?"* → `check_novelty`
- *"Pull the latest from r/SomebodyMakeThis"* → `fetch_reddit`

## Configuration

All optional — `fetch_hackernews` and `check_novelty` work with zero configuration. Set the rest in a `.env` file at the repo root (or export them in the environment):

| Variable | Required for | Notes |
|----------|--------------|-------|
| `ANTHROPIC_API_KEY` | `detect_gaps` | Without it, the tool tells your agent how to do the analysis itself using `fetch_hackernews` |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT` | `fetch_reddit` | Create a script app at reddit.com/prefs/apps |

## Development

```bash
uv run pytest          # tests
uv run mypy src/       # type check (strict)
ruff check --fix . && ruff format .
```

Layout: `src/idea_forge/` holds the pipeline layers (`ingestion/`, `gaps/`, …) and `mcp_server.py`, the stdio MCP entry point. Tests mirror `src/` under `tests/`.
