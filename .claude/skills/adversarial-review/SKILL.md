---
name: adversarial-review
description: Review โค้ดที่เปลี่ยนแบบ adversarial — หาบั๊กจริงพร้อม failure scenario ใช้เมื่อผู้ใช้ขอ review หรือก่อน commit งานสำคัญ
---

# /adversarial-review

1. หา scope: `git diff` (uncommitted) หรือ diff เทียบ main — ถ้าผู้ใช้ระบุไฟล์/commit ใช้ตามนั้น
2. Spawn subagent `reviewer` พร้อม scope + สเปคที่เกี่ยว (ถ้ามีใน `specs/`)
3. ตรวจ findings ที่ได้ **ด้วยตัวเองทีละข้อ** — อ่านโค้ดจริงประกอบ ยืนยันหรือหักล้าง อย่าส่งต่อ finding ที่ตัวเองยังไม่เชื่อ
4. รายงานเฉพาะที่ยืนยันแล้ว เรียงตามความรุนแรง พร้อม file:line และ failure scenario
5. ถ้าผู้ใช้ยังไม่ได้ขอให้แก้ — รายงานอย่างเดียว หยุดตรงนั้น
