---
name: debug
description: Debug อย่างมีระบบ reproduce → isolate → fix → regression test ใช้เมื่อมีบั๊ก, เทสต์ fail, หรือพฤติกรรมไม่ตรงคาด
---

# /debug — มีระบบ ไม่เดา

1. **Reproduce**: หาวิธีทำให้บั๊กเกิดซ้ำได้ (คำสั่ง/เทสต์/input) — ถ้า reproduce ไม่ได้ ห้ามแก้ ให้เก็บข้อมูลเพิ่ม
2. **Isolate**: ตามหลักฐานจริง (log, stack trace, `git log` ว่าอะไรเพิ่งเปลี่ยน, print/breakpoint) จนชี้ root cause ได้เป็น file:line — ห้ามแก้ตามสมมุติฐานที่ยังไม่ยืนยัน
3. **Regression test ก่อนแก้**: เขียนเทสต์ที่ fail เพราะบั๊กนี้ (ใช้แนวทาง /test-first)
4. **Fix**: แก้ root cause ไม่ใช่อาการ — ถ้า fix ลาม >3 ไฟล์ หยุดคิดก่อนว่าวินิจฉัยถูกไหม
5. **Verify**: เทสต์ใหม่ผ่าน + suite ทั้งหมดผ่าน
6. รายงาน: root cause, ทำไมมันเกิด, fix, เทสต์กันซ้ำ
