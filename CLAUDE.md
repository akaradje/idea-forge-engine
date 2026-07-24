# Idea Forge Engine

เครื่องมือแตกไอเดียนวัตกรรมจากข้อมูลจริง (research/patents/demand signals) ผ่าน pipeline 6 ชั้น:
Ingestion → Knowledge Engine → Gap Detection → Idea Synthesis → Novelty Validation → Scoring
รายละเอียดสถาปัตยกรรม: `idea-forge-engine-architecture.md`

## Stack
Python 3.12+ / FastAPI / PostgreSQL + pgvector / Claude API — จัดการ dependencies ด้วย `uv` เท่านั้น (ห้าม pip install ตรง ๆ)

## Commands
- Install/sync: `uv sync`
- Run tests: `uv run pytest`
- Single test: `uv run pytest tests/test_x.py::test_name -x`
- Lint+format: `ruff check --fix . && ruff format .` (hook ทำให้อัตโนมัติอยู่แล้วเมื่อแก้ไฟล์)
- Type check: `uv run mypy src/`
- Dev server: `uv run fastapi dev src/idea_forge/main.py`

## Layout
- `src/idea_forge/` — application code (แบ่ง module ตามชั้นของ pipeline: `ingestion/`, `knowledge/`, `gaps/`, `synthesis/`, `novelty/`, `scoring/`)
- `tests/` — mirror โครงสร้าง src
- `.claude/` — Claude Code config (agents, skills, rules, hooks)

## Hard rules
- Secrets อยู่ใน `.env` เท่านั้น (hook บล็อกการเขียน key ลงไฟล์อยู่แล้ว)
- ทุก external API call (arXiv, Reddit, ฯลฯ) ต้องมี timeout + retry และห้าม block event loop — ใช้ async
- โค้ดใหม่ทุกชิ้นต้องมีเทสต์ — งาน implement ใช้ workflow `/implement` (spec → test → code → review)
- ตัดสินใจเรื่องสถาปัตยกรรม/สเปคใน main session (Fable) เท่านั้น; subagent `coder` ห้ามออกแบบเอง
