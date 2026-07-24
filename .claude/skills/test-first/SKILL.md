---
name: test-first
description: เขียนเทสต์ก่อนแก้โค้ด (red → green) ใช้กับ bug fix ทุกตัว และฟีเจอร์เล็กที่ไม่ต้องผ่าน /spec เต็มรูป
---

# /test-first — red ก่อน green

1. เขียนเทสต์ที่จับพฤติกรรมที่ต้องการ (หรือ reproduce บั๊ก) ตาม rules ใน `.claude/rules/testing.md`
2. รันให้เห็นว่า **fail ด้วยเหตุผลที่ถูกต้อง** — ถ้า fail เพราะ import error/typo แก้เทสต์ก่อน ห้ามแตะ src/ จนกว่าจะได้ red ที่แท้จริง
3. แก้โค้ดให้เทสต์ผ่าน — แก้น้อยที่สุดที่ทำให้ green
4. รันเทสต์ทั้ง suite (`uv run pytest`) กัน regression
5. รายงาน: เทสต์ที่เพิ่ม, ผล red→green, ไฟล์ที่แก้
