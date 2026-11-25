# xau-agent: Claude วิเคราะห์ทองและเข้า order ผ่าน MT5 (XM)

> ⚠️ โปรเจกต์ทดลอง (vibe code) ใช้กับ **บัญชี DEMO** ก่อนเสมอ ไม่ใช่คำแนะนำการลงทุน
> ระบบนี้ไม่มีอะไรรับประกันกำไร และ scalping เป้าเล็กกว่า spread มักขาดทุนจากต้นทุนธุรกรรม

## โครงสร้าง

```
MT5 terminal (XM) ◄── broker.py (Gateway: ที่เดียวที่คุยกับโบรก)
                          ▲
                     tools.py (ToolExecutor) ── risk.py (กฎตายตัว, Claude แก้ไม่ได้)
                          ▲                 └── journal.py (SQLite: ทุก tool call / order)
                     agent.py (Claude tool-use loop, 1 รอบต่อแท่งเทียนที่ปิด)
                          ▲
                     main.py (รอแท่ง M5 ปิด → รัน cycle, kill switch)
```

Claude มี tools เท่านี้: `get_market_snapshot`, `get_account_state`, `propose_order`, `close_position`
และ `propose_order` **ทุกครั้ง** ต้องผ่าน `RiskEngine` ก่อน:

- บังคับใส่ SL + TP และต้องอยู่ฝั่งที่ถูกต้อง
- ระยะ SL ต่ำสุด/สูงสุด, reward:risk ขั้นต่ำ
- spread สูงสุด, ช่วงเวลาเทรด, ช่วงงดข่าว (กรอกเองใน `.env`; ระบบไม่มี news feed)
- ขาดทุนต่อวันสูงสุด (เทียบ equity ต้นวัน UTC), จำนวน position และจำนวนเทรดต่อวัน
- ลด lot อัตโนมัติให้ความเสี่ยงต่อไม้ ≤ % ของ equity และ ≤ `MAX_LOT`
- 1 proposal ต่อรอบ, ปิดได้เฉพาะ position ที่ agent เปิดเอง (magic number)

## โหมด

| MODE | ทำอะไร |
|---|---|
| `advisory` (ค่าเริ่มต้น) | วิเคราะห์ + `order_check` กับโบรก **ไม่ส่ง order จริง** |
| `demo` | ส่ง order ได้ แต่โปรแกรมจะไม่ยอมเริ่มถ้าบัญชีไม่ใช่ demo |
| `live` | บัญชีจริง ต้องตั้ง `ALLOW_REAL_ACCOUNT=true` เพิ่ม |

## ติดตั้ง (Windows, ต้องมี MT5 ติดตั้งและ login ได้)

```powershell
cd D:\xau-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

จากนั้นแก้ `.env` เอง: ใส่ `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `SYMBOL`, `ANTHROPIC_API_KEY`
(ไฟล์ `.env` ถูก gitignore ไว้แล้ว ห้าม commit)

ใน MT5: Tools → Options → Expert Advisors → เปิด **Allow algorithmic trading**

## ใช้ Claude Code แทน API (ใช้โควตาแพ็กเกจ ไม่ต้องเติมเครดิต)

ตั้ง `LLM_BACKEND=claude-code` ใน `.env` แล้วทุกรอบโปรแกรมจะเรียก `claude -p` (headless)
Claude Code ถูกอนุญาตให้รันได้แค่ `python -m xau_agent.cli ...` และทุก order ยังผ่าน risk engine เหมือนเดิม
(โปรแกรมลบ `ANTHROPIC_API_KEY` ออกจาก environment ของ Claude Code เพื่อบังคับให้ใช้แพ็กเกจ ไม่ใช้เครดิต)

ติดตั้งและ login ครั้งเดียว (Claude Code บน Windows ต้องมี Git for Windows):

```powershell
npm install -g @anthropic-ai/claude-code
claude
```

ในหน้าต่าง `claude` ให้เลือก login ด้วยบัญชี Claude (แพ็กเกจ Pro/Max) แล้วพิมพ์ `/exit`

- `CLAUDE_CODE_MODEL` เว้นว่าง = โมเดลตั้งต้น, ใส่ `sonnet` หรือ `haiku` เพื่อประหยัดโควตา
- โควตาแพ็กเกจมีจำกัดต่อช่วง 5 ชม. และต่อสัปดาห์ รันทุก M5 ทั้งวันอาจชนเพดาน พิจารณา `DECISION_TIMEFRAME=M15`
- ใช้กับบัญชีของตัวเองเท่านั้น ถ้าจะเปิดเป็นบริการให้คนอื่นต้องใช้ API

ทดสอบ tools ด้วยมือได้: `python -m xau_agent.cli snapshot` / `account`

## รัน

```powershell
python -m xau_agent.main --check   # ทดสอบการเชื่อมต่อ ดู symbol/spread ไม่เรียก Claude
python -m xau_agent.main --once    # ให้ Claude วิเคราะห์ 1 รอบ
python -m xau_agent.main           # รันต่อเนื่องทุกแท่ง DECISION_TIMEFRAME
```

**Kill switch**: สร้างไฟล์ชื่อ `STOP` ในโฟลเดอร์นี้ → หยุดรอบใหม่และ risk engine ปฏิเสธทุก order
ลบไฟล์เพื่อทำงานต่อ (position ที่เปิดอยู่ยังมี SL/TP ฝั่งเซิร์ฟเวอร์โบรก)

## ทดสอบ (ไม่ต้องมี MT5 หรือ API key)

```powershell
python -m unittest discover -s tests -t .
```

## Log สำหรับดูย้อนหลังและเทรน

ทุกรอบถูกเก็บอัตโนมัติ 3 ชั้น:

| ที่เก็บ | มีอะไร |
|---|---|
| `logs/cycles/YYYY-MM-DD.jsonl` | transcript เต็มของแต่ละรอบ: ข้อความที่ส่งให้ Claude, สรุปเหตุผล (thinking summary), ทุก tool call + ผลลัพธ์ (รวม snapshot กราฟที่โมเดลเห็นจริง), การกระทำ, token, latency |
| `logs/prompts/<version>.json` | system prompt + tools แต่ละเวอร์ชัน (แต่ละรอบอ้าง `prompt_version`) ใช้เทียบผลเมื่อแก้ prompt |
| `journal.sqlite` | `cycles` (1 แถว/รอบ), `orders`, `labels`, `events` (audit ละเอียด) |

**Label ผลลัพธ์อัตโนมัติ** (หลังแต่ละรอบ และสั่งเองได้ด้วย `python -m xau_agent.main --label`)
เมื่อเวลาผ่านไปครบ `SIM_MAX_MINUTES` ระบบจะดึงแท่ง M1 มาคำนวณ:

- **ทุกรอบ (รวม NO TRADE)**: ราคาขึ้นสูงสุด/ลงต่ำสุด/ปิด หลังตัดสินใจ 15 และ 60 นาที (points)
  ใช้ตอบว่า "ที่ไม่เข้าถูกไหม"
- **ทุก order ที่ผ่าน risk (รวม advisory)**: จำลองว่า SL หรือ TP โดนก่อน, กำไร/ขาดทุน (points), MFE/MAE
  ถ้า SL และ TP อยู่ในแท่งเดียวกันจะนับเป็น SL (มองแง่ร้ายไว้ก่อน)
- **order ที่ส่งจริง**: กำไรสุทธิจริงจาก MT5 (รวม commission/swap) และเหตุผลที่ปิด (sl/tp/manual)

ข้อจำกัด: การจำลองใช้แท่ง M1 (ราคา bid) + spread ตอนตัดสินใจ จึงไม่รวม slippage
และ label ได้เฉพาะรอบที่ยังอยู่ในแท่ง M1 ย้อนหลัง ~3000 แท่ง (ถ้าปิดโปรแกรมนานเกิน รอบเก่าจะได้ status `no_data`)

**Export**

```powershell
python -m xau_agent.dataset          # exports/cycles.csv (เปิดใน Excel) + exports/train.jsonl
python -m xau_agent.dataset --all    # รวมรอบที่ยังไม่ได้ label
```

- `cycles.csv`: 1 แถว/รอบ ดูภาพรวมได้เร็ว: decision, SL/TP, ผลจำลอง, P&L จริง, ราคาหลัง 15/60 นาที
- `train.jsonl`: transcript เต็ม + label ต่อรอบ สำหรับวิเคราะห์ ปรับ prompt หรือทำ eval

> ถ้าจะใช้ transcript ที่ Claude สร้างไปเทรนโมเดล AI อื่น ให้ตรวจเงื่อนไขการใช้งานของ Anthropic ก่อน
> ส่วนข้อมูลราคาและ label ผลลัพธ์เป็นข้อมูลของคุณเอง

## Backtest และ swing bot (H4 Donchian 20/10)

ผล backtest ทั้งหมดอยู่ใน `strategy/CHANGELOG.md` — setup M5 ของ agent ไม่มี edge หลังหักต้นทุน,
ส่วน H4 Donchian 20/10 มี edge เล็กแต่สม่ำเสมอ (2013–2026: +0.13R/ไม้, กำไร 12 จาก 14 ปี)

```
python -m xau_agent.history      # ดึงประวัติ M5/M1 จาก MT5 ไว้ที่ data/
python -m xau_agent.backtest     # ทดสอบ setup M5 ของ playbook
python -m xau_agent.swing        # ทดสอบระบบ D1/H4 (ข้อมูล 2001–2026)
python -m xau_agent.swing_bot --check    # ดูสัญญาณ H4 ปัจจุบัน ไม่ส่งออเดอร์
python -m xau_agent.swing_bot            # รัน swing bot (MODE=demo) แยก process, magic 260513
python -m xau_agent.swing_bot --report   # ผลไม้ที่ปิดแล้ว เทียบกับค่าคาดหวังจาก backtest
```

swing bot ทำตามกฎเดียวกับ backtest เป๊ะ (ไม่มี LLM veto เพื่อให้วัดผลเทียบได้): stop ที่โบรก 2x ATR20,
ไม่มี TP, ออกเมื่อปิดหลุด channel 10 แท่ง ไฟล์ `STOP` กันการเข้าไม้ใหม่ (การออกและ stop ยังทำงาน)

## Dashboard (ไฟล์ .html)

```
python -m xau_agent.dashboard --watch    # เขียน dashboard.html ใหม่ทุก 30 วินาที (pid: logs/dashboard.pid)
```

เปิดไฟล์ `D:\xau-agent\dashboard.html` ใน browser ได้เลย หน้าเว็บรีโหลดตัวเองทุก 30 วินาที แสดง pipeline
ทีละขั้น (ข้อมูลตลาด → ข่าว → Agent → Risk engine → Opus → โบรก + Swing H4) และแต่ละรอบว่า agent สั่งเครื่องมืออะไร
ได้ผลอะไร และสรุปว่าอย่างไร อ่านอย่างเดียว ไม่มีปุ่มสั่งเทรด

## ลำดับที่แนะนำ

1. `--check` บนบัญชี demo → ตรวจชื่อ symbol, spread จริง, tick value
2. `MODE=advisory` อย่างน้อย 2–4 สัปดาห์ แล้วดูว่าคำแนะนำใน `orders` ทำผลได้อย่างไรหลังหัก spread
3. `MODE=demo` ด้วย `MAX_LOT=0.01`
4. ค่อยพิจารณาบัญชีจริงเมื่อผลบน demo มีหลักฐานพอ

## หมายเหตุ Claude API

- โมเดลเริ่มต้น `claude-opus-5-5`, `CLAUDE_EFFORT=low` เพื่อลด latency (รอบละหลายวินาที ไม่เหมาะกับการยิงระดับ tick)
- เปิด server-side refusal fallback (`CLAUDE_FALLBACKS=default`); ตั้ง `off` เพื่อปิด
- ค่าใช้จ่าย: รอบละหลายพัน token × ทุก 5 นาที ดู usage ได้ในตาราง `events` (kind = `llm_response`)
