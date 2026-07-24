# Dogfood run: Idea Forge วิเคราะห์โดเมนตัวเอง (2026-07-24)

รัน pipeline กับโดเมน idea-tooling เอง (HN Algolia, queries: "validate startup idea", "market research tool", "find startup ideas" — 36 โพสต์) โดยใช้ Claude Code เป็นชั้นวิเคราะห์แทน API

## ข้อค้นพบหลัก

### 1. "Idea validator" คือทะเลแดงที่ไม่มี demand จริง
คู่แข่งตรง ≥4 ราย ทุกตัว traction ต่ำมากบน HN (1-3 pts, 0-1 comments):
- Starts Club — YC-style validation reports (`hackernews:47199149`, `47198748`)
- "idea validator for founders who move fast" (`hackernews:48607210`)
- "Validate your startup idea in 14 days" (`hackernews:48761442`)
- Backlash ชัดเจน: "Stop asking AI if your startup idea is good" (`hackernews:48653414`)

### 2. Engagement ไหลไปที่ agent tooling ไม่ใช่รายงานให้คนอ่าน
- Spine Swarm (multi-agent canvas ทำ competitive analysis) — 109 pts / 69 comments (`hackernews:47364116`)
- OneCLI (credential vault สำหรับ agent) — 102 pts (`hackernews:49023427`)
- **Finterm.ai — "Bloomberg terminal for Claude Code"** ขาย data access ให้ coding agents (`hackernews:48896257`)
- AgentCash — agents จ่ายเงินซื้อ premium API ได้แล้ว = ช่องเก็บเงินจาก agent ตรง ๆ (`hackernews:47325628`)

## การตัดสินใจ (approved by user)

**Idea A — กลับหัว positioning: MCP server คือตัวผลิตภัณฑ์** (confidence 0.8)
Idea Forge = "demand-signal terminal for Claude Code" ไม่ใช่ dashboard รายวันให้คนอ่าน
- ขัดเกลา MCP server เป็นผลิตภัณฑ์ก่อนทำ Knowledge Engine/pgvector (สลับลำดับ roadmap V2 เดิม)
- เพิ่ม tools: `detect_gaps` + `check_novelty` เข้า MCP → ปล่อย open source ทดสอบตลาดแบบ Finterm

## ไอเดียสำรอง (ยังไม่ตัดสิน)
- **B. Gap velocity alerts** (confidence 0.65) — เฝ้า gap ต่อเนื่อง แจ้งเตือนเมื่อ demand เร่งตัว (ScoutFox `hackernews:49020185` ยังเล็ก ช่องเปิด)
- **C. Evidence-only positioning** (confidence 0.6) — รายงานไม่มีคะแนนความเห็น AI มีแต่หลักฐานตรวจสอบได้ (สอดคล้อง backlash ข้อ 1)

## บริบทประกอบจากรันก่อนหน้า (Ask HN สด 30 โพสต์)
Gap candidates ที่ตรวจ novelty แล้ว: CI write-budget gate (novelty ผ่าน — คู่แข่งใกล้สุดคือ Bencher custom metrics ซึ่งเป็น generic platform), SSD wear community index, registrar account-takeover monitoring, human-escalation guarantee tooling
