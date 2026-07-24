---
name: ship
description: ปิดงานก่อน commit — เช็คลิสต์คุณภาพครบแล้ว commit ใช้เมื่องานเสร็จและผู้ใช้พร้อมบันทึก
---

# /ship — ปิดงาน

รันเช็คลิสต์ตามลำดับ ข้อไหน fail หยุดแล้วแก้ก่อน:

1. `uv run pytest` — ผ่านทั้งหมด
2. `uv run mypy src/` — สะอาด
3. `ruff check .` — สะอาด
4. `git status` + `git diff` — ตรวจว่าทุกไฟล์ที่เปลี่ยนตั้งใจเปลี่ยนจริง ไม่มีไฟล์หลง/debug code/TODO ที่ค้าง
5. Commit: message รูปแบบ `<layer>: <what changed>` (เช่น `gaps: add demand-supply gap scorer`) อธิบาย "ทำไม" ใน body ถ้าไม่ trivial
6. สรุปให้ผู้ใช้: commit hash, ไฟล์ที่เข้า, สิ่งที่เหลือ (ถ้ามี)
