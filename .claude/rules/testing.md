---
paths:
  - "tests/**/*.py"
---

# Testing conventions

- โครงสร้าง tests/ mirror src/ (เช่น `src/idea_forge/gaps/detector.py` → `tests/gaps/test_detector.py`)
- ชื่อเทสต์บอกพฤติกรรม: `test_gap_score_is_zero_when_supply_exceeds_demand`
- Mock เฉพาะ boundary ภายนอก (HTTP, DB, Claude API) ด้วย `respx`/fixtures — ห้าม mock โค้ดภายในของเราเอง
- ทุก bug fix ต้องมี regression test ที่ fail ก่อนแก้
- เทสต์ external API ใช้ recorded fixtures (JSON ตัวอย่างจริงใน `tests/fixtures/`) ไม่ยิง network จริง
- Async tests ใช้ pytest-asyncio (auto mode เปิดอยู่แล้วใน pyproject)
