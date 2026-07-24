---
paths:
  - "src/idea_forge/ingestion/**"
---

# Data Ingestion rules

- ทุก source (arXiv, Reddit, Google Trends, ฯลฯ) implement `SourceAdapter` Protocol เดียวกัน: `fetch() -> list[RawDocument]`
- เคารพ rate limits ของแต่ละ API — กำหนด limiter ต่อ source, ห้าม parallel เกินโควตา
- ข้อมูลดิบเก็บพร้อม metadata เสมอ: `source`, `fetched_at`, `source_url`, `raw_payload` — ห้ามทิ้งข้อมูลต้นทาง
- Ingestion ต้อง idempotent: รันซ้ำวันเดียวกันไม่สร้าง record ซ้ำ (dedup ด้วย content hash)
- Failure ของ source หนึ่งห้ามล้ม pipeline ทั้งเส้น — isolate ต่อ source แล้ว log
