---
paths:
  - "src/**/*.py"
---

# Python conventions (src/)

- Async ทั้งเส้นสำหรับ I/O: `httpx.AsyncClient`, `asyncpg`/SQLAlchemy async — ห้าม `requests` และห้าม sync DB call ใน request path
- ทุก external call ต้องกำหนด `timeout` ชัดเจน และ retry ด้วย `tenacity` (exponential backoff, max 3)
- Pydantic v2 models สำหรับทุก boundary: API request/response, ข้อมูลจากแหล่งภายนอก, config (`pydantic-settings` อ่านจาก .env)
- Type hints ครบทุก function signature — mypy strict ต้องผ่าน
- Error handling: จับ exception เฉพาะเจาะจง, log ด้วย `structlog` พร้อม context (source, layer), ห้าม `except Exception: pass`
- แต่ละชั้นของ pipeline (ingestion/knowledge/gaps/synthesis/novelty/scoring) พึ่งพากันผ่าน interface (Protocol) เท่านั้น — ห้าม import ข้ามชั้นแบบลึก
