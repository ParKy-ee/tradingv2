# ติดตั้งบนเครื่องสำหรับรันจริง (runtime PC)

> ⚠️ **รันบอทได้ทีละเครื่องเท่านั้น** ถ้าเครื่องเก่าและเครื่องใหม่รันพร้อมกันบนบัญชีเดียวกัน จะเข้าไม้ซ้ำสองเท่า
> (magic number เหมือนกัน) ให้หยุดเครื่องเก่าก่อน: `powershell -ExecutionPolicy Bypass -File scripts\stop_all.ps1`

## 1. โปรแกรมที่ต้องมี (Windows 64-bit)

| โปรแกรม | หมายเหตุ |
|---|---|
| Python 3.10+ (64-bit) | ติ๊ก "Add python.exe to PATH" ตอนติดตั้ง |
| Git for Windows | ใช้ clone และ Claude Code ต้องใช้ |
| MetaTrader 5 ของ XM | login บัญชี **demo** แล้วเปิด Tools → Options → Expert Advisors → **Allow algorithmic trading** |
| Node.js LTS + Claude Code | `npm install -g @anthropic-ai/claude-code` แล้วรัน `claude` ครั้งหนึ่งเพื่อ login บัญชี Claude (แพ็กเกจ) จากนั้น `/exit` |

## 2. ดึงโค้ดและติดตั้ง

```powershell
cd D:\
git clone https://github.com/<บัญชีของคุณ>/xau-agent.git
cd xau-agent
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## 3. ไฟล์ที่ไม่ได้อยู่ใน GitHub (ต้องย้ายเอง)

| ไฟล์ | ทำไม | วิธีย้าย |
|---|---|---|
| `.env` | มีรหัส MT5 และค่าตั้งทั้งหมด ห้ามขึ้น GitHub | คัดลอกผ่าน USB หรือแชร์ไฟล์ในเครือข่ายบ้าน แล้วเช็ค `MT5_PATH` ให้ตรงกับที่ติดตั้ง MT5 บนเครื่องใหม่ |
| `journal.sqlite` | ประวัติทุกรอบ, order, ความเห็น AI, ข่าว | ไม่บังคับ แต่ควรย้ายเพื่อให้สถิติและการเทียบผล forward test ต่อเนื่อง (ย้ายตอนบอทหยุดแล้ว) |
| `logs\seen_*.txt` | แท่ง H4 ล่าสุดที่บอทเห็น ใช้ตรวจสัญญาณที่พลาด | ย้ายพร้อม journal |
| `data\` | ข้อมูลราคาย้อนหลังสำหรับ backtest | ไม่ต้องย้าย สร้างใหม่ได้ด้วย `python -m xau_agent.history` |

## 4. ตรวจก่อนรัน

```powershell
.venv\Scripts\python -m xau_agent.main --check                 # เชื่อม MT5 ได้, บัญชีเป็น demo, เห็น GOLD
.venv\Scripts\python -m xau_agent.swing_bot --check            # สัญญาณ H4 ของ GOLD
.venv\Scripts\python -m unittest discover -s tests -t .        # test ทั้งหมดผ่าน
```

## 5. รัน / หยุด

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1   # เริ่มทุกตัว (ข้ามตัวที่รันอยู่แล้ว)
powershell -ExecutionPolicy Bypass -File scripts\stop_all.ps1    # หยุดทุกตัว (ไม้ที่เปิดอยู่ยังมี SL ที่โบรก)
```

เริ่ม 5 process: `awake` (กันจอดับ/standby), M5 agent (โหมดตาม `.env`, ตอนนี้ advisory), swing GOLD (demo),
swing BTCUSD (demo), dashboard (`dashboard.html` อัปเดตทุก 30 วินาที)

## 6. ตั้งค่าเครื่องให้รันได้ตลอด

- เสียบไฟตลอด และตั้ง Settings → System → Power → "Sleep" เมื่อเสียบสาย = **Never**
- เครื่องที่ใช้ Modern Standby จะพักโปรแกรมเมื่อจอดับ `awake` เปิดจอค้างไว้กันปัญหานี้ (หรือตั้ง Screen timeout = Never)
- ตั้งให้ Windows Update ไม่ restart เองช่วงตลาดเปิด (Active hours)
- ถ้าอยากให้บอทเริ่มเองหลังเปิดเครื่อง: Task Scheduler → Create Task → trigger "At log on" →
  action `powershell -ExecutionPolicy Bypass -File D:\xau-agent\scripts\start_all.ps1` (MT5 ต้องเปิดและ login อยู่)

## 7. อัปเดตโค้ดภายหลัง

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop_all.ps1
git pull
.venv\Scripts\pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
```

## 8. ทำงานจากที่ไหนก็ได้ผ่าน Tailscale (SSH + dashboard บนมือถือ)

เครื่อง runtime ตอนนี้: **parky — 100.81.86.78** (Tailscale)

1. ติดตั้ง Tailscale ทั้งเครื่อง runtime และเครื่องที่ใช้ทำงาน/มือถือ แล้ว login บัญชีเดียวกัน
2. บนเครื่องที่ใช้ทำงาน สร้าง SSH key (ตั้ง passphrase ได้ตามต้องการ):
   ```powershell
   ssh-keygen -t ed25519
   ```
   คัดลอกไฟล์ `%USERPROFILE%\.ssh\id_ed25519.pub` (ไฟล์ **.pub** เท่านั้น ห้ามส่งไฟล์ที่ไม่มี .pub) ไปที่เครื่อง runtime
3. บนเครื่อง runtime เปิด PowerShell แบบ **Run as administrator**:
   ```powershell
   cd D:\xau-agent
   powershell -ExecutionPolicy Bypass -File scripts\runtime_setup.ps1 -PublicKeyFile C:\path\id_ed25519.pub
   ```
   เปิด OpenSSH Server, firewall รับ SSH/dashboard **เฉพาะจาก Tailscale (100.64.0.0/10)**, ปิด login ด้วยรหัสผ่าน,
   สร้าง task `xau-start` (เริ่มบอทเองตอน login) และ `xau-stop`
4. ทดสอบจากเครื่องที่ใช้ทำงาน: `ssh <ชื่อผู้ใช้ Windows>@100.81.86.78`
5. Claude desktop app: เพิ่ม SSH connection ไปที่ `100.81.86.78` แล้วเปิด session ที่โฟลเดอร์ `D:\xau-agent`
   เพื่อให้ Claude แก้โค้ด ดู log และรัน test บนเครื่อง runtime ได้โดยตรง

**สั่งบอทผ่าน SSH:** ใช้ task เพื่อให้บอทรันในหน้าจอ desktop (MT5 ต้องอยู่ใน session เดียวกัน)
```powershell
schtasks /run /tn xau-stop
schtasks /run /tn xau-start
```
**Dashboard จากมือถือ:** เปิด `http://100.81.86.78:8765/` (มือถือต้องเปิด Tailscale) — ส่งเฉพาะไฟล์ dashboard
ไม่มีทางเข้าถึง `.env` หรือไฟล์อื่น และฟังเฉพาะ IP ของ Tailscale

ข้อควรรู้: เครื่อง runtime ต้อง login Windows ค้างไว้ (MT5 ต้องมีหน้าจอ desktop) — ใช้ awake กันจอดับ/standby
