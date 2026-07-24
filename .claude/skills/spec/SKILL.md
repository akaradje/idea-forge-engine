---
name: spec
description: สร้างสเปคละเอียดสำหรับฟีเจอร์ใหม่ก่อนเขียนโค้ด ใช้เมื่อผู้ใช้ขอฟีเจอร์/การเปลี่ยนแปลงที่แตะมากกว่า 1-2 ไฟล์ หรือมีการตัดสินใจเชิงออกแบบ
---

# /spec — ออกแบบก่อนเขียน

1. ทำความเข้าใจ requirement จากผู้ใช้ ถ้ามีจุดตัดสินใจสำคัญที่เลือกแทนไม่ได้ ใช้ AskUserQuestion ก่อน
2. Spawn subagent `architect` พร้อม requirement + บริบทไฟล์ที่เกี่ยว ให้ผลิตสเปคตาม template ของมัน (Problem / Files / Interfaces / Behavior & edge cases / Acceptance criteria / Non-goals / Order)
3. **ตรวจสเปคเองใน main session** (คุณคือ Fable — ฉลาดกว่า architect ที่รันบน Opus ก็ตรวจซ้ำได้): หา edge case ที่หลุด, criteria ที่วัดไม่ได้, scope ที่บวมเกิน แก้ให้เรียบร้อย
4. บันทึกสเปคเป็นไฟล์ `specs/<date>-<slug>.md` แล้วสรุปให้ผู้ใช้ พร้อมเสนอขั้นต่อไป: `/implement specs/<file>`
