"""Monitoring dashboard as a single self-contained HTML file (read-only).

    python -m xau_agent.dashboard            # write dashboard.html once
    python -m xau_agent.dashboard --watch    # rewrite it every 30 s (open the file in a browser; it reloads itself)

Shows the decision pipeline stage by stage (market -> news -> agent -> risk engine -> Opus -> broker, plus the
H4 swing bot), and for every cycle what the agent did: the commands it ran, what the tools returned, and its
final reasoning. Data comes from MT5 (live quote/account/positions), journal.sqlite and logs/cycles/*.jsonl.
Nothing here can place or change orders.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dashboard.html"


def _alive(pidfile: str) -> bool | None:
    p = ROOT / "logs" / pidfile
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except ValueError:
        return None
    import subprocess
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
    return str(pid) in r.stdout


def _steps(events: list[dict]) -> list[dict[str, Any]]:
    """Claude Code stream-json events -> readable steps."""
    out = []
    for e in events:
        if e.get("type") == "init":
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            t = b.get("type")
            if t == "text" and b.get("text"):
                out.append({"kind": "text", "text": b["text"]})
            elif t == "thinking":
                out.append({"kind": "thinking", "text": b.get("thinking") or "(thinking hidden by the CLI)"})
            elif t == "tool_use":
                inp = b.get("input") or {}
                out.append({"kind": "tool", "text": inp.get("command") or json.dumps(inp, ensure_ascii=False)})
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                text = str(c or "")
                try:
                    text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
                out.append({"kind": "result", "text": text[:6000], "error": bool(b.get("is_error"))})
    return out


def _transcripts(ids: set[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for f in sorted(glob.glob(str(ROOT / "logs" / "cycles" / "*.jsonl")), key=os.path.getmtime, reverse=True)[:3]:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if not any(i in line[:200] for i in ids):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("cycle_id") in ids:
                    found[rec["cycle_id"]] = {"user_prompt": rec.get("user_prompt", ""),
                                              "steps": _steps(rec.get("events") or [])}
        if len(found) == len(ids):
            break
    return found


def _payload(p: str) -> Any:
    try:
        v = json.loads(p)
        return json.loads(v) if isinstance(v, str) else v
    except (ValueError, TypeError):
        return p


def collect(broker=None, n_cycles: int = 40) -> dict[str, Any]:
    from . import news
    db = sqlite3.connect(f"file:{ROOT / 'journal.sqlite'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    cycles = [dict(r) for r in db.execute(
        "SELECT cycle_id, ts, decision, model, summary, error, bid, ask, spread_points, latency_ms, prompt_version "
        "FROM cycles ORDER BY ts DESC LIMIT ?", (n_cycles,))]
    tr = _transcripts({c["cycle_id"] for c in cycles[:15]})
    for c in cycles:
        c.update(tr.get(c["cycle_id"], {}))
    orders = [dict(r) for r in db.execute(
        "SELECT ts, cycle_id, order_type, side, price, entry_ref, lot, sl, tp, approved, sent, ok, ticket, reasons, "
        "agent_reason FROM orders ORDER BY ts DESC LIMIT 40")]
    events = [{"ts": r["ts"], "cycle_id": r["cycle_id"], "kind": r["kind"], "payload": _payload(r["payload"])}
              for r in db.execute("SELECT ts, cycle_id, kind, payload FROM events WHERE kind IN "
                                  "('order_review','pending_cancelled','news_fetch','swing','swing_error','cycle_error',"
                                  "'llm_error','sweep_error','mtf_error','news_error') ORDER BY id DESC LIMIT 60")]
    try:
        upcoming = [dict(r) for r in db.execute(
            "SELECT event_epoch, title, impact, forecast, previous FROM news_events WHERE gold_relevant = 1 "
            "AND event_epoch BETWEEN ? AND ? ORDER BY event_epoch",
            (int(now.timestamp()) - 3 * 3600, int((now + timedelta(hours=48)).timestamp())))]
    except sqlite3.OperationalError:
        upcoming = []
    forward = [dict(r) for r in db.execute(
        "SELECT ts, cycle_id, side, price, entry_ref, sl, lot, ticket, ok FROM orders "
        "WHERE cycle_id LIKE 'swing-%' AND sent = 1 ORDER BY ts DESC LIMIT 30")]
    shadow = {r["cycle_id"]: _payload(r["payload"]) for r in db.execute(
        "SELECT cycle_id, payload FROM events WHERE kind = 'shadow_review' ORDER BY id")}
    for f in forward:
        f["symbol"] = "GOLD" if len(f["cycle_id"].split("-")) == 2 else f["cycle_id"].split("-")[1]
        f["ai"] = shadow.get(f["cycle_id"])
    missed = [{"ts": r["ts"], "payload": _payload(r["payload"])} for r in db.execute(
        "SELECT ts, payload FROM events WHERE kind IN ('swing_missed') ORDER BY id DESC LIMIT 20")]
    data: dict[str, Any] = {
        "forward": forward, "missed": missed,
        "generated_utc": now.isoformat(), "processes": {"m5_agent": _alive("agent.pid"), "swing_bot": _alive("swing.pid"),
                                                     "swing_btc": _alive("swing_btcusd.pid")},
        "stop_file": (ROOT / "STOP").exists(), "cycles": cycles, "orders": orders, "events": events,
        "news": upcoming, "market": None, "account": None, "positions": [], "pending": [], "stats": None,
        "swing": None,
    }
    if broker is not None:
        try:
            from .broker import build_snapshot
            from .stats import stats_text
            from .swing_bot import signal
            from .tools import SNAPSHOT_TIMEFRAMES
            from .journal import Journal
            snap = build_snapshot(broker, SNAPSHOT_TIMEFRAMES)
            data["market"] = {"quote": snap["quote"], "mtf": snap["mtf_alignment"],
                              "tf": {k: {x: v.get(x) for x in ("trend", "ema20", "ema50", "atr14", "rsi14",
                                                                   "range_high", "range_low")}
                                     for k, v in snap["timeframes"].items()}}
            a = broker.account()
            data["account"] = {"login": a.login, "type": a.trade_mode, "balance": a.balance, "equity": a.equity}
            data["positions"] = [p.__dict__ for p in broker.positions()]
            data["pending"] = [o.__dict__ for o in broker.pending_orders()]
            j = Journal(str(ROOT / "journal.sqlite"))
            try:
                data["stats"] = stats_text(j, broker)
                news_block = news.blackout(j, now)
            finally:
                j.close()
            data["news_block"] = news_block
            h4 = broker.bars("H4", 80)[:-1]
            sw_pos = next((p for p in data["positions"] if p["magic"] == broker.magic + 1), None)
            sig = signal(h4, sw_pos["side"] if sw_pos else None)
            for f in data["forward"]:
                if f["ok"] and f["ticket"]:
                    try:
                        d = broker.deal_history(int(f["ticket"])) or {}
                        f["closed"], f["net"] = d.get("closed"), d.get("net_profit")
                    except Exception:
                        pass
            data["swing"] = {"position": sw_pos, "signal": sig,
                             "bar_time": datetime.fromtimestamp(sig["bar_time"], tz=timezone.utc).strftime("%m-%d %H:%M")}
        except Exception as exc:  # dashboard must never crash the watcher
            data["market_error"] = repr(exc)
    db.close()
    return data


def render(data: dict[str, Any]) -> str:
    blob = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    return HTML.replace("__DATA__", blob)


def write(data: dict[str, Any]) -> None:
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(render(data), encoding="utf-8")
    os.replace(tmp, OUT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--every", type=int, default=30)
    a = ap.parse_args()
    broker = None
    try:
        from .broker import MT5Broker
        from .config import load_settings
        s = load_settings()
        broker = MT5Broker(s.symbol, s.magic, s.mt5_login, s.mt5_password, s.mt5_server, s.mt5_path)
    except Exception as exc:
        print(f"MT5 unavailable, dashboard shows journal data only: {exc}")
    while True:
        try:
            write(collect(broker))
            print(f"[{datetime.now():%H:%M:%S}] wrote {OUT}", flush=True)
        except Exception as exc:
            print(f"dashboard error: {exc}", flush=True)
        if not a.watch:
            break
        time.sleep(a.every)


HTML = r"""<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XAU Agent Monitor</title>
<style>
:root{--bg:#0f1512;--card:#17201b;--card2:#1d2822;--line:#2a3a31;--text:#e6efe9;--muted:#8fa69a;--ok:#3fbf7f;--warn:#e0a83a;--bad:#e0605a;--idle:#6f8479;--accent:#5fd39b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI","Noto Sans Thai",Tahoma,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0;font-weight:600}h2{font-size:15px;margin:0 0 8px;font-weight:600}
.muted{color:var(--muted)}.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.idle{color:var(--idle)}
main{padding:16px;display:grid;gap:16px}
.pipe{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.stage{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--idle);border-radius:10px;padding:10px 12px;min-height:118px}
.stage.ok,.stage.warn,.stage.bad,.stage.idle{color:var(--text)}.stage.ok{border-top-color:var(--ok)}.stage.warn{border-top-color:var(--warn)}.stage.bad{border-top-color:var(--bad)}
.stage .n{font-size:12px;color:var(--muted)}.stage .t{font-weight:600;margin:2px 0 6px}.stage .v{font-size:13px;word-break:break-word}
.grid{display:grid;grid-template-columns:minmax(280px,380px) 1fr;gap:16px}@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.tl{max-height:640px;overflow:auto;display:flex;flex-direction:column;gap:4px}
.row{display:grid;grid-template-columns:54px 92px 1fr;gap:8px;align-items:center;overflow:hidden;padding:6px 8px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.row:hover{background:var(--card2)}.row.sel{border-color:var(--accent);background:var(--card2)}
.dec{font-size:12px;padding:1px 6px;border-radius:6px;background:#24312a;text-align:center}
.dec.buy,.dec.buy_limit,.dec.buy_stop{background:#183a2a;color:#7ee2ad}.dec.sell,.dec.sell_limit,.dec.sell_stop{background:#3a1f1e;color:#f0a19c}
.dec.error{background:#4a1f1d;color:#ffb3ae}.dec.close{background:#3a321a;color:#f3d38a}
.step{border-left:3px solid var(--line);padding:6px 10px;margin:6px 0;background:var(--card2);border-radius:0 8px 8px 0}
.step.tool{border-left-color:#6aa8ff}.step.result{border-left-color:#7c8c84}.step.text{border-left-color:var(--accent)}.step.thinking{border-left-color:#b48ce0}
.step .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
pre{white-space:pre-wrap;word-break:break-word;margin:4px 0 0;font:12px/1.45 Consolas,monospace;color:#cfe0d6}
.md{white-space:pre-wrap}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
details summary{cursor:pointer;color:var(--muted)}label{font-size:13px}
.flowwrap{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 14px 10px}
.flow{display:flex;align-items:center;gap:0;overflow-x:auto;padding:6px 2px 10px}
.node{flex:0 0 auto;min-width:112px;max-width:150px;background:#121a16;border:1px solid var(--line);border-radius:10px;padding:8px 10px;text-align:center;position:relative;transition:all .3s}
.node .ic{font-size:18px;line-height:1}.node .nm{font-size:12.5px;font-weight:600;margin-top:4px}.node .sb{font-size:11px;color:var(--muted)}
.node.visited{border-color:#2f6b4c;background:#14241b}.node.blocked{border-color:var(--warn);background:#2a2414}.node.error{border-color:var(--bad);background:#2a1716}
.node.active{border-color:var(--accent);box-shadow:0 0 0 2px rgba(95,211,155,.25),0 0 18px rgba(95,211,155,.35);background:#163024}
.node.active::after{content:"";position:absolute;inset:-5px;border-radius:13px;border:2px solid var(--accent);opacity:.6;animation:pulse 1.4s ease-out infinite}
@keyframes pulse{0%{transform:scale(.97);opacity:.7}100%{transform:scale(1.08);opacity:0}}
.edge{flex:1 0 22px;min-width:22px;height:2px;background:repeating-linear-gradient(90deg,var(--line) 0 6px,transparent 6px 10px);margin:0 2px}
.edge.on{background:repeating-linear-gradient(90deg,var(--accent) 0 6px,transparent 6px 10px);background-size:20px 2px;animation:flow .6s linear infinite}
@keyframes flow{to{background-position:20px 0}}
@media (prefers-reduced-motion:reduce){.node.active::after,.edge.on{animation:none}}
.now{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;margin-top:4px}.now .big{font-size:16px;font-weight:600}
.feed{max-height:190px;overflow:auto;margin-top:8px;font-size:12.5px}.feed div{padding:2px 0;border-bottom:1px dashed #22302a}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px;animation:blink 1.2s infinite}@keyframes blink{50%{opacity:.25}}
.stale .live{background:var(--warn);animation:none}

</style></head><body>
<header><div><h1>XAU Agent Monitor <span class="muted" style="font-weight:400">· demo</span></h1>
<div class="muted" id="gen"></div></div>
<div id="procs"></div>
<a href="stocks.html" style="color:var(--accent)">หุ้นสหรัฐ + มุมมอง AI →</a> <label><input type="checkbox" id="auto" checked> รีเฟรชอัตโนมัติทุก 30 วินาที</label></header>
<main>
<section class="flowwrap" id="livem5"><h2><span class="live"></span>ตอนนี้บอท M5 อยู่ขั้นไหน <span class="muted" style="font-weight:400" id="m5age"></span></h2>
<div class="flow" id="flowm5"></div><div class="now" id="nowm5"></div><details><summary>ลำดับการทำงานล่าสุด (live)</summary><div class="feed" id="feedm5"></div></details></section>
<section class="flowwrap" id="liveswing"><h2><span class="live"></span>Swing bot H4 <span class="muted" style="font-weight:400" id="swage"></span></h2>
<div class="flow" id="flowsw"></div><div class="now" id="nowsw"></div></section>
<section class="flowwrap" id="livebtc"><h2><span class="live"></span>Swing bot H4 · BTCUSD (forward test) <span class="muted" style="font-weight:400" id="btcage"></span></h2>
<div class="flow" id="flowbtc"></div><div class="now" id="nowbtc"></div></section>
<section><h2>สรุปรอบล่าสุด (ทุก 30 วินาที)</h2><div class="pipe" id="pipe"></div></section>
<section class="grid"><div class="card"><h2>รอบการตัดสินใจ (M5)</h2><div class="tl" id="tl"></div></div>
<div class="card"><h2 id="dtitle">รายละเอียด</h2><div id="detail" class="muted">เลือกรอบทางซ้าย</div></div></section>
<section class="card" style="margin-bottom:16px"><h2>Swing H4 (ทดสอบจริงบน demo) + ความเห็น Shadow AI</h2>
<div class="muted" style="margin-bottom:6px">AI ให้ความเห็นทุกไม้แต่ไม่มีผลกับไม้จริง ใช้เทียบทีหลังว่าถ้าทำตาม AI จะดีกว่ากฎไหม (ตัดสินหลังครบ ~30 ไม้)</div>
<div id="fwd"></div><div id="missed" style="margin-top:8px"></div></section>
<section class="cols">
<div class="card"><h2>ไม้เปิด / คำสั่งรอ</h2><div id="pos"></div></div>
<div class="card"><h2>ผลแยกตาม setup</h2><pre id="stats"></pre></div>
<div class="card"><h2>ข่าวที่เกี่ยวกับทอง (48 ชม.)</h2><div id="news"></div></div>
</section>
<section class="cols"><div class="card"><h2>คำสั่งล่าสุด</h2><div id="orders"></div></div>
<div class="card"><h2>เหตุการณ์ระบบ (Opus, ยกเลิก pending, ข่าว, swing, error)</h2><div id="events" style="max-height:420px;overflow:auto"></div></div></section>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const th=ts=>{if(!ts)return'';const d=new Date(ts);return d.toLocaleString('th-TH',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'short',hour12:false})};
const ago=ts=>{const m=Math.round((Date.now()-new Date(ts))/60000);return m<1?'เมื่อสักครู่':m<60?m+' นาทีที่แล้ว':Math.round(m/60)+' ชม.ที่แล้ว'};
const setupOf=s=>{s=String(s||'');const m=s.match(/Setup(?: is)?:?\**\s*(RANGE_FADE|BREAKOUT|PULLBACK|NONE)/i)||s.match(/\b(RANGE_FADE|BREAKOUT|PULLBACK)\b/);return m?m[1].toUpperCase().replace('NONE',''):''};
$('gen').textContent='อัปเดต '+th(D.generated_utc)+' · '+ago(D.generated_utc);
const pr=D.processes||{};const st=v=>v===true?'<span class="ok">● ทำงาน</span>':v===false?'<span class="bad">● หยุด</span>':'<span class="idle">● ไม่ทราบ</span>';
$('procs').innerHTML=`<span class="pill">บอท M5 ${st(pr.m5_agent)}</span> <span class="pill">Swing H4 ${st(pr.swing_bot)}</span> <span class="pill">Swing BTC ${st(pr.swing_btc)}</span> `+(D.stop_file?'<span class="pill bad">STOP file เปิดอยู่</span>':'');
const last=(D.cycles||[])[0]||{};const lastOrder=(D.orders||[]).find(o=>o.cycle_id===last.cycle_id)||(D.orders||[])[0];
const review=(D.events||[]).find(e=>e.kind==='order_review');
const stages=[];
const m=D.market;
stages.push({n:'1',t:'ข้อมูลตลาด',c:m?'ok':'bad',v:m?`Bid ${m.quote.bid} / Ask ${m.quote.ask}<br>Spread ${m.quote.spread_points} จุด<br><span class="muted">${esc(m.mtf.line)}</span>`:esc(D.market_error||'ไม่มีข้อมูล MT5')});
const nb=D.news_block;const nx=(D.news||[]).find(e=>e.event_epoch*1000>Date.now());
stages.push({n:'2',t:'ข่าว',c:nb?'warn':'ok',v:nb?esc(nb):(nx?`ถัดไป: ${esc(nx.title)} (${esc(nx.impact)})<br>${th(nx.event_epoch*1000)}`:'ไม่มีข่าวสำคัญใน 48 ชม.')});
const lc=last.error?'bad':(Date.now()-new Date(last.ts)>12*60000?'warn':'ok');
stages.push({n:'3',t:'Agent วิเคราะห์ (Sonnet)',c:lc,v:`${th(last.ts)} · <b>${esc(last.decision)}</b> ${esc(setupOf(last.summary))}<br><span class="muted">${esc((last.error||'').slice(0,90)||ago(last.ts))}</span>`});
let rc='idle',rv='รอบนี้ไม่มีข้อเสนอ';if(lastOrder&&lastOrder.cycle_id===last.cycle_id){rc=lastOrder.approved?'ok':'warn';rv=lastOrder.approved?`ผ่าน: ${esc(lastOrder.order_type||'market')} ${esc(lastOrder.side)}`:'ปฏิเสธ: '+esc((lastOrder.reasons||'').slice(0,120))}
else if(lastOrder){rv=`ล่าสุด ${th(lastOrder.ts)}: ${lastOrder.approved?'ผ่าน':'ปฏิเสธ'} ${esc(lastOrder.order_type||'')}`}
stages.push({n:'4',t:'Risk engine',c:rc,v:rv});
stages.push({n:'5',t:'Opus ตรวจทาน',c:review?(review.payload.verdict==='approve'?'ok':'warn'):'idle',v:review?`${th(review.ts)} · <b>${review.payload.verdict==='approve'?'อนุมัติ':'ปฏิเสธ'}</b><br><span class="muted">${esc((review.payload.reason||'').slice(0,140))}</span>`:'ยังไม่มีการตรวจ'});
const a=D.account;const np=(D.positions||[]).length,nq=(D.pending||[]).length;
stages.push({n:'6',t:'โบรก / ออเดอร์',c:a?'ok':'bad',v:a?`Equity ${a.equity.toFixed(2)}<br>ไม้เปิด ${np} · คำสั่งรอ ${nq}<br><span class="muted">${esc(a.type)} #${a.login}</span>`:'ไม่มีข้อมูล'});
const sw=D.swing;stages.push({n:'H4',t:'Swing bot (Donchian 20/10)',c:pr.swing_bot===false?'bad':(sw&&sw.position?'ok':'idle'),v:sw?`แท่งล่าสุด ${esc(sw.bar_time)} (server)<br>สถานะ: ${sw.position?esc(sw.position.side)+' @ '+sw.position.price_open:'ไม่มีไม้'}<br>สัญญาณ: ${esc(sw.signal.entry||'ไม่มี')}${sw.signal.channel?` <span class="muted">กรอบ ${sw.signal.channel.map(x=>x.toFixed(1)).join(' – ')}</span>`:''}`:'ไม่มีข้อมูล'});
$('pipe').innerHTML=stages.map(s=>`<div class="stage ${s.c}"><div class="n">ขั้น ${s.n}</div><div class="t">${s.t}</div><div class="v">${s.v}</div></div>`).join('');
function showCycle(id){const c=D.cycles.find(x=>x.cycle_id===id);if(!c)return;document.querySelectorAll('.row').forEach(r=>r.classList.toggle('sel',r.dataset.id===id));
location.hash=id;$('dtitle').textContent='รอบ '+th(c.ts)+' · '+c.decision;
const ords=D.orders.filter(o=>o.cycle_id===id),revs=D.events.filter(e=>e.cycle_id===id);let h='';
h+=`<div class="muted">model ${esc(c.model)} · ${c.latency_ms?Math.round(c.latency_ms/1000)+' วินาที':''} · spread ${esc(c.spread_points)} · prompt ${esc(c.prompt_version)}</div>`;
if(c.error)h+=`<div class="step"><div class="k bad">error</div><pre>${esc(c.error)}</pre></div>`;
if(c.user_prompt)h+=`<details><summary>สิ่งที่ส่งให้ agent รอบนี้ (prompt ต่อรอบ)</summary><pre>${esc(c.user_prompt)}</pre></details>`;
const kn={tool:'สั่งเครื่องมือ',result:'ผลจากเครื่องมือ',text:'ข้อสรุปของ agent',thinking:'การคิด'};
(c.steps||[]).forEach(s=>{const body=s.kind==='result'?`<details><summary>${esc(s.text.slice(0,90))}…</summary><pre>${esc(s.text)}</pre></details>`:s.kind==='text'?`<div class="md">${esc(s.text)}</div>`:`<pre>${esc(s.text)}</pre>`;
h+=`<div class="step ${s.kind}"><div class="k">${kn[s.kind]||s.kind}</div>${body}</div>`});
if(!(c.steps||[]).length)h+=`<div class="step text"><div class="k">ข้อสรุปของ agent</div><div class="md">${esc(c.summary)}</div><div class="muted">(ไม่มี transcript ละเอียดสำหรับรอบเก่า)</div></div>`;
ords.forEach(o=>h+=`<div class="step"><div class="k">Risk engine</div>${o.approved?'<span class="ok">ผ่าน</span>':'<span class="warn">ปฏิเสธ</span>'} ${esc(o.order_type||'market')} ${esc(o.side)} lot ${esc(o.lot)} SL ${esc(o.sl)} TP ${esc(o.tp)} ${o.ticket?'· ticket '+o.ticket:''}<pre>${esc(o.reasons||'')}</pre></div>`);
revs.filter(e=>e.kind==='order_review').forEach(e=>h+=`<div class="step"><div class="k">Opus</div><b>${e.payload.verdict==='approve'?'อนุมัติ':'ปฏิเสธ'}</b> · ${Math.round((e.payload.latency_ms||0)/1000)} วินาที<div class="md">${esc(e.payload.reason)}</div></div>`);
$('detail').innerHTML=h;$('detail').classList.remove('muted')}
$('tl').innerHTML=(D.cycles||[]).map(c=>`<div class="row" data-id="${c.cycle_id}"><span class="muted">${th(c.ts).split(' ').pop()}</span><span class="dec ${esc(c.decision)}">${esc(c.decision)}</span><span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(setupOf(c.summary))} <span class="muted">${esc((c.summary||'').replace(/\*\*/g,'').replace(/Decision:/,'').slice(0,60))}</span></span></div>`).join('');
document.querySelectorAll('.row').forEach(r=>r.onclick=()=>showCycle(r.dataset.id));
showCycle(location.hash.slice(1)||last.cycle_id);
const P=[...(D.positions||[]).map(p=>`<tr><td>${p.magic===260513?'Swing H4':'M5'}</td><td>${esc(p.side)}</td><td>${p.volume}</td><td>${p.price_open}</td><td>SL ${p.sl} / TP ${p.tp}</td><td class="${p.profit>=0?'ok':'bad'}">${p.profit.toFixed(2)}</td></tr>`),
...(D.pending||[]).map(o=>`<tr><td>รอ</td><td>${esc(o.order_type)}</td><td>${o.volume}</td><td>${o.price}</td><td>SL ${o.sl} / TP ${o.tp}</td><td></td></tr>`)];
$('pos').innerHTML=P.length?`<table><tr><th>บอท</th><th>ประเภท</th><th>lot</th><th>ราคา</th><th>SL/TP</th><th>กำไร</th></tr>${P.join('')}</table>`:'<span class="muted">ไม่มี</span>';
$('stats').textContent=D.stats||'ไม่มีข้อมูล';
$('news').innerHTML=(D.news||[]).length?'<table>'+D.news.map(e=>`<tr><td>${th(e.event_epoch*1000)}</td><td class="${e.impact==='High'?'warn':''}">${esc(e.impact)}</td><td>${esc(e.title)}<br><span class="muted">คาด ${esc(e.forecast||'-')} · ก่อนหน้า ${esc(e.previous||'-')}</span></td></tr>`).join('')+'</table>':'<span class="muted">ไม่มี</span>';
$('orders').innerHTML='<table><tr><th>เวลา</th><th>คำสั่ง</th><th>ราคา</th><th>ผล</th></tr>'+(D.orders||[]).slice(0,15).map(o=>`<tr><td>${th(o.ts)}</td><td>${esc(o.order_type||'market')} ${esc(o.side)}${String(o.cycle_id).startsWith('swing')?' <span class="pill">swing</span>':''}</td><td>${esc(o.price??o.entry_ref)}</td><td>${o.approved?(o.ok?'<span class="ok">ส่งแล้ว</span>':'<span class="idle">ไม่ได้ส่ง</span>'):'<span class="warn">ปฏิเสธ</span> <span class="muted">'+esc((o.reasons||'').slice(0,70))+'</span>'}</td></tr>`).join('')+'</table>';
const V={take:'<span class="ok">เข้า</span>',half:'<span class="warn">ครึ่งเดียว</span>',skip:'<span class="bad">งด</span>'};
$('fwd').innerHTML=(D.forward||[]).length?'<table><tr><th>เวลา</th><th>Symbol</th><th>ไม้</th><th>ราคาเข้า / SL</th><th>ผล</th><th>AI</th><th>เหตุผล AI</th></tr>'+(D.forward||[]).map(f=>`<tr><td>${th(f.ts)}</td><td>${esc(f.symbol)}</td><td>${esc(f.side)} ${esc(f.lot)}</td><td>${esc(f.price??f.entry_ref)} / ${esc(f.sl)}</td><td>${!f.ok?'<span class="bad">ส่งไม่สำเร็จ</span>':f.closed?`<b class="${f.net>=0?'ok':'bad'}">${(+f.net).toFixed(2)}</b>`:'<span class="idle">ถืออยู่</span>'}</td><td>${f.ai?(f.ai.verdict?V[f.ai.verdict]+' '+esc(f.ai.confidence)+'%':'<span class="bad">error</span>'):'<span class="muted">รอ</span>'}</td><td class="muted">${esc(((f.ai&&(f.ai.reason||f.ai.error))||'').slice(0,220))}</td></tr>`).join('')+'</table>':'<span class="muted">ยังไม่มีไม้ swing</span>';
$('missed').innerHTML=(D.missed||[]).length?'<b class="warn">สัญญาณที่พลาดเพราะเครื่องปิด/หลับ:</b> '+D.missed.map(m=>`${esc(m.payload.symbol)} ${esc(m.payload.side)} @ ${esc(m.payload.close)} <span class="muted">(${th(m.ts)})</span>`).join(' · '):'<span class="muted">ไม่มีสัญญาณที่พลาด</span>';
const evT={order_review:'Opus',pending_cancelled:'ยกเลิก pending',news_fetch:'ดึงข่าว',swing:'Swing'};
$('events').innerHTML='<table>'+(D.events||[]).map(e=>{const p=e.payload;let s=typeof p==='object'?(p.verdict?(p.verdict+' · '+(p.reason||'')):p.reason?(p.reason+' ticket '+(p.ticket||'')):p.action?(p.action+(p.entry?' '+p.entry:'')):JSON.stringify(p)):p;
return `<tr><td>${th(e.ts)}</td><td class="${/error/.test(e.kind)?'bad':''}">${evT[e.kind]||e.kind}</td><td>${esc(String(s).slice(0,220))}</td></tr>`}).join('')+'</table>';

const M5N=[['wait_candle','⏱','รอแท่ง M5','ทุก 5 นาที'],['market','📈','ข้อมูลตลาด','ราคา + 5 TF'],['account','🧾','บัญชี + ข่าว','ไม้เปิด, ข่าว'],['agent','🧠','Sonnet วิเคราะห์','ตาม playbook'],['risk','🛡','Risk engine','กฎตายตัว'],['opus','🔍','Opus ตรวจทาน','ทุกออเดอร์'],['broker','🏦','ส่ง MT5','demo'],['done','✅','จบรอบ','บันทึก log']];
const SWN=[['wait_h4','⏱','รอแท่ง H4','ทุก 4 ชม.'],['broker','🏦','ส่ง/ปิด MT5','ตามสัญญาณ'],['position','📌','ถือไม้','stop ที่โบรก']];
const STL={wait_candle:'รอแท่ง M5 ปิด',market:'ดึงข้อมูลตลาด',account:'ตรวจบัญชีและข่าว',news:'ติดช่วงข่าว',agent:'Sonnet กำลังวิเคราะห์',risk:'Risk engine ตรวจ',opus:'Opus กำลังตรวจทาน',broker:'ส่งคำสั่งไปโบรก',done:'จบรอบ',wait_h4:'รอแท่ง H4 ปิด',position:'ถือไม้อยู่'};
const secs=ts=>Math.max(0,Math.round((Date.now()-new Date(ts))/1000));
const dur=s=>s<60?s+' วินาที':s<3600?Math.floor(s/60)+' นาที '+(s%60)+' วินาที':Math.floor(s/3600)+' ชม. '+Math.floor(s%3600/60)+' นาที';
function drawFlow(el,nodes,st){if(!st){el.innerHTML='<span class="muted">ยังไม่มีสถานะ (บอทยังไม่ได้รายงาน)</span>';return}
 const cur=st.stage==='news'?'account':st.stage;const cyc=st.cycle_id;const feed=(st.feed||[]).filter(f=>cyc&&f.cycle_id===cyc);
 const visited=new Set(feed.map(f=>f.stage==='news'?'account':f.stage));const bad={};feed.forEach(f=>{if(f.state==='blocked'||f.state==='error')bad[f.stage==='news'?'account':f.stage]=f.state});
 const ai=nodes.findIndex(n=>n[0]===cur);
 el.innerHTML=nodes.map((n,i)=>{let c='';if(n[0]===cur)c=st.state==='blocked'?'blocked':st.state==='error'?'error':'active';else if(bad[n[0]])c=bad[n[0]];else if(visited.has(n[0])&&cur!=='wait_candle')c='visited';
  const edge=i<nodes.length-1?`<div class="edge ${i<ai&&cur!=='wait_candle'?'on':''}"></div>`:'';
  return `<div class="node ${c}"><div class="ic">${n[1]}</div><div class="nm">${n[2]}</div><div class="sb">${n[3]}</div></div>${edge}`}).join('')}
function renderLive(){const m=window.XAU_STATUS_M5,w=window.XAU_STATUS_SWING,bt=window.XAU_STATUS_SWING_BTCUSD;
 drawFlow($('flowm5'),M5N,m);drawFlow($('flowsw'),SWN,w);drawFlow($('flowbtc'),SWN,bt);
 if(bt){$('nowbtc').innerHTML=`<span class="big">${STL[bt.stage]||bt.stage}</span><span>${esc(bt.detail)}</span><span class="muted">มา ${dur(secs(bt.since))}</span>`;$('btcage').textContent='อัปเดต '+th(bt.since)}
 if(m){const a=secs(m.since);$('livem5').classList.toggle('stale',secs((m.feed||[{}])[0].ts||m.since)>420&&m.stage!=='wait_candle');
  let extra='';if(m.stage==='wait_candle'&&m.next_at){const left=Math.max(0,Math.round((new Date(m.next_at)-Date.now())/1000));extra=` · แท่งถัดไปใน <b>${dur(left)}</b>`;
   if(m.last_result)extra+=`<br><span class="muted">รอบล่าสุด: <b>${esc(m.last_result.decision)}</b> ${esc((m.last_result.summary||'').replace(/\*\*/g,'').slice(0,160))}</span>`}
  $('nowm5').innerHTML=`<span class="big ${m.state==='blocked'?'warn':m.state==='error'?'bad':'ok'}">${STL[m.stage]||m.stage}</span><span>${esc(m.detail)}</span><span class="muted">มา ${dur(a)}${m.cycle_id&&m.stage!=='wait_candle'?' · รอบ '+esc(m.cycle_id).slice(8,12):''}</span>${extra}`;
  $('m5age').textContent='อัปเดต '+th(m.since);
  $('feedm5').innerHTML=(m.feed||[]).map(f=>`<div><span class="muted">${new Date(f.ts).toLocaleTimeString('th-TH',{hour12:false})}</span> · <b class="${f.state==='blocked'?'warn':f.state==='error'?'bad':''}">${STL[f.stage]||f.stage}</b> · ${esc(f.detail)}</div>`).join('')}
 if(w){$('nowsw').innerHTML=`<span class="big">${STL[w.stage]||w.stage}</span><span>${esc(w.detail)}</span><span class="muted">มา ${dur(secs(w.since))}</span>`;$('swage').textContent='อัปเดต '+th(w.since)}}
function loadStatus(){['m5','swing','swing_btcusd'].forEach(c=>{const old=document.getElementById('st_'+c);const sc=document.createElement('script');sc.id='st_'+c;sc.src='status_'+c+'.js?'+Date.now();
 sc.onload=()=>{renderLive();if(old)old.remove()};sc.onerror=()=>{if(old)old.remove()};document.body.appendChild(sc)})}
loadStatus();setInterval(loadStatus,2000);setInterval(renderLive,1000);
let auto=true;try{auto=localStorage.getItem('xau_auto')!=='0'}catch(e){}$('auto').checked=auto;
$('auto').onchange=e=>{try{localStorage.setItem('xau_auto',e.target.checked?'1':'0')}catch(_){}};
setTimeout(()=>{if($('auto').checked)location.reload()},30000);
</script></body></html>
"""

if __name__ == "__main__":
    main()
