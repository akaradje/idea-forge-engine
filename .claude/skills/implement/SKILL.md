---
name: implement
description: รัน pipeline เต็มเส้น spec → test → code → adversarial review → fix → verify ใช้เมื่อมีสเปคแล้ว (จาก /spec) หรือผู้ใช้สั่ง implement ฟีเจอร์ที่ requirement ชัด
---

# /implement — pipeline มาตรฐาน

รับ argument: path ของสเปค (หรือ requirement ที่ชัดพอ — ถ้าไม่ชัด ให้รัน /spec ก่อน)

1. **Fan-out ขนานกัน** (ส่งสเปคเต็ม ๆ ให้ทั้งคู่ ในข้อความเดียว):
   - subagent `test-writer` → เขียนเทสต์จาก acceptance criteria
   - subagent `coder` → implement ตามสเปค
2. **Integrate**: รัน `uv run pytest` — ถ้าเทสต์กับโค้ดไม่ลงรอย ตัดสินจากสเปค (สเปคคือ source of truth) แล้วส่งงานแก้ให้ `coder`
3. **Review**: subagent `reviewer` ตรวจ diff ทั้งหมดเทียบสเปค
4. **Fix loop**: findings ระดับ CONFIRMED ส่งให้ `coder` แก้ — สูงสุด 2 รอบ ถ้ายังไม่จบ ยกให้ผู้ใช้ตัดสิน
5. **Verify ใน main session**: รัน `uv run pytest` + `uv run mypy src/` ด้วยตัวเอง — เชื่อผลที่ตัวเองเห็น ไม่เชื่อรายงาน subagent
6. สรุปให้ผู้ใช้: อะไรเสร็จ, ผลเทสต์จริง, findings ที่แก้/ข้าม, ขั้นถัดไป (`/ship`)
