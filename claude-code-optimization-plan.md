# แผนแม่บท: Claude Code Optimization สำหรับ Idea Forge Engine

> เป้าหมาย: ยกระดับ environment ของ Claude Code ให้เทียบเท่าทีมพัฒนามืออาชีพ — โดยใช้ **Fable 5 เป็นสถาปนิก/ผู้ตรวจสอบ** และโมเดลรอง (Sonnet/Haiku) เป็นผู้เขียนโค้ดตามสเปค
> อัปเดต: กรกฎาคม 2026

---

## 0. ภาพรวมสถาปัตยกรรมการตั้งค่า

```
Idea Forge Engine/
├── CLAUDE.md                      # สั้น ≤60 บรรทัด — ภาพรวม + คำสั่งหลัก
├── .claude/
│   ├── settings.json              # permissions, hooks, env (commit เข้า repo)
│   ├── settings.local.json        # ค่าเฉพาะเครื่อง (gitignore)
│   ├── rules/                     # path-scoped rules — โหลดเมื่อแตะไฟล์ที่เกี่ยว
│   │   ├── frontend.md
│   │   ├── backend.md
│   │   └── testing.md
│   ├── skills/                    # workflow ที่เรียกซ้ำได้ (/spec, /implement, ...)
│   │   ├── spec/SKILL.md
│   │   ├── implement/SKILL.md
│   │   ├── adversarial-review/SKILL.md
│   │   ├── test-first/SKILL.md
│   │   └── ship/SKILL.md
│   ├── agents/                    # subagent definitions พร้อม model routing
│   │   ├── architect.md           # model: fable/opus
│   │   ├── coder.md               # model: sonnet
│   │   ├── reviewer.md            # model: fable/opus
│   │   ├── test-writer.md         # model: sonnet
│   │   └── grunt.md               # model: haiku
│   └── workflows/                 # multi-agent orchestration scripts
│       └── feature-pipeline.js
└── .mcp.json                      # MCP servers ระดับโปรเจกต์
```

หลักการ progressive disclosure: **CLAUDE.md โหลดเสมอ (จ่ายแพงทุก turn) → rules โหลดตาม path → skills โหลดตามงาน → agents โหลดเฉพาะใน subagent**

---

## เฟส 1 — Foundation (สัปดาห์ 1): CLAUDE.md + Settings + Permissions

### 1.1 CLAUDE.md (ทำก่อนทุกอย่าง)
- รัน `/init` แล้วตัดให้เหลือเฉพาะสิ่งที่ "โมเดลเดาเองไม่ได้":
  - คำสั่ง build / test / lint ที่ถูกต้อง (คำสั่งเดียวต่อบรรทัด)
  - โครงสร้าง directory ระดับบนสุด + ไฟล์ entry point
  - ข้อห้ามเด็ดขาด (เช่น ห้ามแก้ generated files, ห้ามแตะ migration เก่า)
- กฎเหล็ก: **≤60 บรรทัด** — ถ้ายาวกว่านั้น ย้ายไป rules/skills
- อย่าใส่: coding style ที่ linter บังคับอยู่แล้ว, ประวัติโปรเจกต์, สิ่งที่อ่านได้จากโค้ด

### 1.2 settings.json — Permissions
```jsonc
{
  "permissions": {
    "allow": [
      "Bash(npm run test:*)", "Bash(npm run lint:*)", "Bash(npm run build)",
      "Bash(git status)", "Bash(git diff:*)", "Bash(git log:*)"
    ],
    "deny": [
      "Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)",
      "Bash(rm -rf:*)", "Bash(git push --force:*)"
    ],
    "ask": ["Bash(git push:*)", "Bash(npm publish:*)"]
  }
}
```
- ใช้ skill `/fewer-permission-prompts` หลังใช้งานไป 1–2 สัปดาห์ เพื่อ mine allowlist จาก transcript จริง
- `deny` สำคัญกว่า `allow` — secrets ต้องถูกบล็อกที่ harness ไม่ใช่หวังพึ่งพรอมต์

### 1.3 Hooks — Guardrails อัตโนมัติ
| Hook | Event | หน้าที่ |
|---|---|---|
| auto-format | `PostToolUse` (Edit/Write) | รัน prettier/ruff เฉพาะไฟล์ที่แก้ |
| lint-gate | `PostToolUse` (Edit/Write) | รัน linter, ส่ง error กลับเข้า context ให้แก้ทันที |
| block-secrets | `PreToolUse` | บล็อกการเขียนไฟล์ที่มี pattern ของ API key |
| test-reminder | `Stop` | เตือนถ้าแก้ src/ แต่ไม่ได้รันเทสต์ในเทิร์นนั้น |
| notify | `Notification` | แจ้งเตือน OS เมื่อรอ input (งาน background ยาว ๆ) |

ตัวอย่าง auto-format hook:
```jsonc
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{ "type": "command",
        "command": "npx prettier --write \"$CLAUDE_FILE_PATHS\" 2>nul" }]
    }]
  }
}
```
> ใช้ skill `update-config` ตอนลงมือจริง เพื่อให้ syntax ตรงกับเวอร์ชันปัจจุบัน

**Definition of Done เฟส 1:** เปิด session ใหม่แล้วโมเดลรู้คำสั่ง build/test ถูกต้องโดยไม่ต้องบอก, ไม่มี permission prompt สำหรับงาน read-only, format อัตโนมัติทุกครั้งที่แก้ไฟล์

---

## เฟส 2 — Model Routing + Subagents (สัปดาห์ 2)

### 2.1 ตารางเลือกโมเดล (Right model for the right job)
| งาน | โมเดล | เหตุผล |
|---|---|---|
| ออกแบบสถาปัตยกรรม, สเปค, debugging ยาก, review | **Fable 5 / Opus** | ต้องการ reasoning สูงสุด |
| เขียนโค้ดตามสเปคชัด, เขียนเทสต์, refactor scoped | **Sonnet** | คุ้มค่าสุดเมื่อสเปคดี |
| rename, สรุป log, ค้นหาไฟล์, งาน mechanical | **Haiku** | เร็ว/ถูก, ไม่ต้องคิดมาก |

- Session หลัก: รันด้วย Fable 5 (คุณคุยกับ "สถาปนิก")
- งาน implement: delegate ลง subagent ที่ frontmatter กำหนด `model: sonnet`
- งานสำรวจ/ค้นหา: ใช้ Explore agent (ไม่กิน context หลัก)

### 2.2 Subagents (`.claude/agents/*.md`)

**architect.md** — `model: opus` (หรือ inherit จาก Fable session)
> วิเคราะห์ requirement → เขียนสเปคละเอียด: ไฟล์ที่ต้องแตะ, interface, edge cases, acceptance criteria, ลำดับการ implement ห้ามเขียนโค้ดเอง — ส่งมอบเป็นสเปคเท่านั้น

**coder.md** — `model: sonnet`
> รับสเปคจาก architect, implement ตามสเปคเป๊ะ ๆ ห้ามออกแบบใหม่/ขยาย scope ถ้าสเปคคลุมเครือให้รายงานกลับแทนการเดา รันเทสต์ก่อนส่งมอบ

**reviewer.md** — `model: opus`, tools: read-only (Read, Grep, Glob, Bash)
> Adversarial review: หา bug จริงที่มี failure scenario ชัดเจน ไม่ใช่ style nit ตรวจว่า implement ตรงสเปคหรือไม่ ต้องพยายาม "หักล้าง" ว่าโค้ดถูก ไม่ใช่ยืนยันว่าถูก

**test-writer.md** — `model: sonnet`
> เขียนเทสต์จาก acceptance criteria ในสเปค **ก่อน** implementation (test-first) ครอบคลุม happy path + edge cases ที่สเปคระบุ

**grunt.md** — `model: haiku`
> งาน mechanical ตามคำสั่งชัดเจน: rename, ย้ายไฟล์, อัปเดต import, แก้ตาม lint

### 2.3 Pipeline มาตรฐานต่อฟีเจอร์
```
คุณ + Fable (main) ── คุยความต้องการ
  → architect        ── สเปค (Fable ตรวจ/แก้สเปคก่อนปล่อย)
  → test-writer      ── เทสต์จาก acceptance criteria   ┐ ขนานกันได้
  → coder            ── implement ตามสเปค              ┘
  → reviewer         ── adversarial review → findings
  → coder            ── แก้ตาม findings ที่ confirmed
  → main session     ── verify: รันเทสต์ทั้งหมด + สรุปให้คุณ
```

**DoD เฟส 2:** ฟีเจอร์ขนาดกลาง 1 ชิ้นวิ่งผ่าน pipeline ครบโดยโค้ด >80% เขียนโดย Sonnet และผ่าน review ของโมเดลใหญ่

---

## เฟส 3 — Skills (สัปดาห์ 3): เข้ารหัส workflow ให้เรียกซ้ำได้

แต่ละ skill = โฟลเดอร์ `.claude/skills/<name>/SKILL.md` (frontmatter: `name`, `description` ที่บอกว่าเมื่อไหร่ควรใช้)

| Skill | หน้าที่ | สาระสำคัญใน SKILL.md |
|---|---|---|
| `/spec` | สร้างสเปคจาก requirement | template สเปค: ปัญหา, ไฟล์ที่แตะ, interface, edge cases, acceptance criteria, non-goals |
| `/implement` | รัน pipeline เฟส 2 ทั้งเส้น | ลำดับ delegate: test-writer + coder → reviewer → fix loop (สูงสุด 2 รอบ) → verify |
| `/adversarial-review` | review เดี่ยว ๆ | เกณฑ์: ทุก finding ต้องมี failure scenario ที่ reproduce ได้; แยก CONFIRMED/PLAUSIBLE |
| `/test-first` | เขียนเทสต์ก่อนโค้ด | ห้ามแตะ src/ จนกว่าเทสต์ fail ด้วยเหตุผลที่ถูกต้อง (red → green) |
| `/ship` | ปิดงาน | checklist: เทสต์ผ่านทั้งหมด, lint สะอาด, ไม่มี TODO ค้าง, สรุป diff, เขียน commit message |
| `/debug` | debugging มีระบบ | reproduce → isolate → hypothesis → verify fix ด้วยเทสต์ regression |

หลักการเขียน SKILL.md:
- description ต้องบอก trigger ชัด ("Use when...") — โมเดลตัดสินใจโหลดจาก description
- ตัว SKILL.md สั้น, รายละเอียด/template แยกเป็นไฟล์ใน `references/` โหลดเมื่อจำเป็น
- ทุก skill จบด้วย verification step ที่รันได้จริง ไม่ใช่ "ตรวจสอบให้เรียบร้อย"

**DoD เฟส 3:** พิมพ์ `/implement <feature>` แล้วได้ pipeline ครบโดยไม่ต้องกำกับทีละขั้น

---

## เฟส 4 — Rules + MCP + Automation (สัปดาห์ 4)

### 4.1 Path-scoped Rules (`.claude/rules/`)
- `frontend.md` — component conventions, state management, ห้าม pattern ไหน
- `backend.md` — error handling, logging, สัญญา API
- `testing.md` — โครงสร้างเทสต์, ห้าม mock อะไร, naming
- แต่ละไฟล์มี frontmatter ระบุ glob (เช่น `paths: ["src/ui/**"]`) — โหลดเฉพาะเมื่อแตะไฟล์นั้น context หลักไม่บวม

### 4.2 MCP (`.mcp.json`) — เพิ่มเฉพาะที่ใช้จริง
- **GitHub MCP** (หรือ `gh` CLI ซึ่งมักพอ) — PR/issues
- **Database MCP** (read-only credential!) — ตรวจ schema/query ตอน debug
- กฎ: ทุก MCP server ที่เพิ่ม = context ที่เสียไป — ถ้า CLI ทำได้ ใช้ CLI

### 4.3 Automation
- **Cron/Schedule**: nightly job รัน `/adversarial-review` บน diff ของวัน + สรุปเป็นรายงาน
- **Hooks ขั้นสูง**: `SessionStart` hook inject สถานะ branch/CI ล่าสุดเข้า context
- **Workflows** (`.claude/workflows/`): เก็บ orchestration script สำหรับงานใหญ่ เช่น audit ทั้ง codebase แบบ fan-out (ใช้เมื่อสั่ง ultracode/workflow เท่านั้น)

**DoD เฟส 4:** context หลักต่อ session ลดลง (วัดจาก CLAUDE.md + rules ที่โหลดจริง), มีรายงาน review อัตโนมัติอย่างน้อยสัปดาห์ละครั้ง

---

## เฟส 5 — Continuous Improvement (ต่อเนื่อง)

1. **Feedback loop รายสัปดาห์**: อะไรที่ต้องพิมพ์สั่งซ้ำ >2 ครั้ง → ย้ายเข้า skill/rule/hook ทันที
2. **Prune รายเดือน**: ลบ rule/skill ที่ไม่ถูกโหลดใน 30 วัน — config ที่บวมแย่พอ ๆ กับไม่มี config
3. **วัดผล**:
   - % โค้ดที่เขียนโดยโมเดลรองแล้วผ่าน review รอบแรก (เป้า >70%)
   - จำนวน permission prompt ต่อ session (เป้า <3)
   - จำนวนครั้งที่ต้องแก้พรอมต์กำกับซ้ำ (เป้า → 0 สำหรับงานประเภทเดิม)
4. **Version ทุกอย่าง**: `.claude/` (ยกเว้น settings.local.json) commit เข้า repo — config คือโค้ด
5. **อัปเกรดตามฟีเจอร์ใหม่**: ถาม claude-code-guide agent เมื่อสงสัยว่ามี capability ใหม่

---

## สรุป Roadmap

| เฟส | ระยะ | ส่งมอบ | ผลลัพธ์หลัก |
|---|---|---|---|
| 1 | สัปดาห์ 1 | CLAUDE.md, permissions, hooks | Guardrails อัตโนมัติ 100% |
| 2 | สัปดาห์ 2 | 5 subagents + model routing | ต้นทุนต่อฟีเจอร์ลดโดยคุณภาพไม่ตก |
| 3 | สัปดาห์ 3 | 6 skills | Workflow เรียกซ้ำได้ด้วยคำสั่งเดียว |
| 4 | สัปดาห์ 4 | rules, MCP, automation | Context เบา + review อัตโนมัติ |
| 5 | ต่อเนื่อง | metrics + pruning | ระบบดีขึ้นเองทุกสัปดาห์ |

ลำดับความสำคัญถ้ามีเวลาจำกัด: **1.1 CLAUDE.md → 1.3 hooks (format+lint) → 2.2 coder+reviewer agents → 3 skill `/implement`** — สี่ชิ้นนี้ให้ผลตอบแทน ~80% ของทั้งแผน
