# Strategy changelog (demo)

## 2026-11-24 00:10 — playbook v1
- Why: first cycles (23:52–00:00) showed the agent correctly identifying a 3657–3681 range but missing
  its own breakout trigger because it only acts at M5 candle closes.
- Change: introduce setup names, pending-order entries, cost floor (~3x spread), ATR-based stops.
- Measure next: share of cycles with a named setup, pending trigger rate, simulated P&L per setup
  (labels arrive 4h after each cycle).

## 2026-11-24 03:30 — ops review 1 (no playbook change)
- Results since v1 (00:15–03:20): 38 cycles = 6 real decisions + 31 errors + 1 order.
  - 31 errors = Claude plan quota exhausted 00:45–03:15 (agent + operator chat share one quota).
  - Order: RANGE_FADE buy_limit 3659.0, SL 3653.5, TP 3668.5 → filled, stopped out in ~4 min,
    exit 3653.33 (17 pts slippage on SL). Net −5.67 USD. One trade: no conclusion about the setup.
- Ops changes:
  - CLAUDE_CODE_MODEL=sonnet (was default) to stretch quota; compare decision quality later by `model`.
  - LLM cycles are skipped outside TRADING_HOURS_UTC when there is no open exposure (saves quota).
  - Labeler runs every 10 min independent of cycles; quota failures tagged `quota_limit`.
- Watch next: whether RANGE_FADE keeps getting run over on range breaks (if 3+ more stop-outs,
  require an M5 rejection candle before fading).

## 2026-11-24 09:00 — ops review 2 (no playbook change)
- 01:00–02:00 UTC (sonnet, 0 errors): 12 cycles, every summary names context + setup. 3 orders filled:
  - PULLBACK buy_limit 3657.5 → TP 3669.5, +12.04
  - BREAKOUT buy_stop 3672.8 (filled 3673.0) → SL 3666.0 in 12 min, −7.01
  - PULLBACK buy_limit 3662.8 → SL 3655.5 in 4 min, −7.30
  - Agent net since start (4 trades, incl. RANGE_FADE −5.67): −7.94 USD. Balance 9026.28.
- Pattern to watch (n is tiny, not acted on): all 3 losers were stopped within 4–12 min with SL ≈ 0.8–0.9x M5 ATR;
  the last PULLBACK bought into a falling M5 leg right after the TP win. If 2+ more PULLBACK losses come while
  M5 RSI is falling / price below M5 EMA20, require an M5 close back above EMA20 before the limit.
- Code: snapshot now loads 200 bars per timeframe (was 60) so EMA50 converges; CLI reloads per call, no restart.

## 2026-11-24 11:15 — ops note before PC shutdown (12:00–18:00 Thai)
- 02:00 UTC PULLBACK buy_limit 3651.5 → SL 3644.5, −7.02. PULLBACK record now 1W/2L (+12.04, −7.30, −7.02).
  One more same-pattern loss triggers the EMA20-reclaim rule above.
- 02:50–03:40 the agent correctly reclassified H1 "nominal TREND" into RANGE and stayed out; 03:45 RANGE_FADE
  buy_limit 3629.6 placed (30 min expiry).
- Agent net since start: −14.96 USD over 5 trades (1W/4L). Balance ≈ 9019.
- Ops gap found: pending expiry is enforced only by our 10s sweep (orders are GTC). If the PC is off, a pending
  order never expires. STOP file is set at 11:55 to cancel pendings before shutdown.
  TODO: use broker-side ORDER_TIME_SPECIFIED expiry when the symbol supports it.
- 11:20 FIXED: pending orders now use ORDER_TIME_SPECIFIED (XM GOLD expiration_mode=15 allows it), expiration
  rounded up to the minute (MT5 floors it). Verified on demo: a 2-min buy_limit with a foreign magic (so our sweep
  ignores it) was removed by the broker on time. The 10s sweep stays as a backup.

## 2026-11-24 14:00 — PC slept instead of shutting down
- The PC went to sleep ~11:35; the scheduled STOP task never fired, so the agent kept running across sleep.
- 04:20 UTC RANGE_FADE buy_limit 3629.6 filled; agent closed it early at 3627.22 on invalidation, −2.37
  (smaller than the SL loss). 04:49 cycle failed with "SSL certificate has expired" (clock right after wake).
- Resumed cleanly at 06:56 UTC. Agent net since start: −17.33 USD over 6 trades (1W/5L).
- Lesson: for a planned absence, create STOP manually (or rely on broker-side expiry, now in place).

## 2026-11-24 16:45 — second sleep, broker-side expiry proven in the wild
- PC slept again ~14:22–16:42 Thai. 07:05 UTC RANGE_FADE buy_limit 3615.5 never filled; XM expired it at
  07:51 UTC (state=EXPIRED, type_time=SPECIFIED) while the PC was asleep — the fix works.
- Agent resumed cleanly at 09:42 UTC (no SSL error this time) and placed a RANGE_FADE sell_limit near range top.

## 2026-11-24 21:05 — playbook v2 + setup-stats feedback
- PC slept again 19:09–20:50 Thai. Keep-awake is now held by the operator session.
- New: each cycle prompt includes realized demo results per setup (stats.py, from MT5 deal history), and the
  per-cycle user prompt is now saved in the transcript (`user_prompt`) for training data.
- Realized (agent only, 8 fills): RANGE_FADE 0W/4L, BREAKOUT 0W/1L, PULLBACK 1W/2L.
- Watch rule from review 1 triggered (3+ more RANGE_FADE losses since 03:30). Every losing RANGE_FADE buy
  faded the range low while M5/M15/H1 EMA20 < EMA50. Change (v2): fade only with the M15 bias, require a
  closed M5 rejection candle first, skip edges tested 3+ times in 2h.
- Measure next: RANGE_FADE placed/filled/win rate under v2 vs v1 (compare by prompt_version).

## 2026-11-24 21:20 — multi-timeframe alignment + daily-count fix
- Snapshot adds M30 and a per-timeframe trend (EMA20 vs EMA50 gap in ATR14 units, ±0.1 = flat, plus price vs
  EMA50), and `mtf_alignment` (e.g. "M1 down | M5 down | M15 down | M30 flat | H1 flat").
- Risk engine: BLOCK_AGAINST_HTF (default on) rejects new entries against M15+M30+H1 when all three agree.
  Note: tonight's losing buys had M15 down but M30/H1 flat, so this hard gate alone would NOT have blocked
  them; the v2 playbook M15 rule covers that case. Measure: how often the gate fires and labels of blocked ideas.
- Bug fix: daily trade cap counted expired pendings and operator test orders. At 21:15 the agent was locked
  out at 10/10 although only 7 trades used risk. Now counts market orders + filled or still-working pendings.

## 2026-11-24 21:20 — Opus reviews every order (user request)
- After the risk engine approves, `reviewer.py` asks Claude Opus (Claude Code CLI, plan billing, no tools) for
  approve/reject with the proposal, agent reason, full snapshot incl. mtf_alignment, setup stats and playbook.
  Reject -> not sent, journal approved=0 with reason "reviewer(opus): ...", event `order_review` logged.
  Fail-closed by default (ORDER_REVIEW_FAIL_OPEN=false): if Opus is unavailable/quota-limited, no order goes out.
- Live check (hypothetical buy_limit at the range low, not sent): Opus rejected in 12 s citing the v2 M15 rule
  and the 3+ tested low — the exact pattern behind today's losses.
- Measure: approve/reject rate, outcome of approved trades vs v2 baseline, and labels of rejected ideas
  (would they have won?). Watch Opus quota use; orders are rare so cost should be small.

## 2026-11-24 21:30 — first backtest (history 2025-06-27 .. 2026-11-24, ~100k M5 bars)
- New: `python -m xau_agent.history` (M5 ~17 months, M1 ~3 months from XM) and `python -m xau_agent.backtest`
  (deterministic approximation of the playbook setups; spread max(bar,55) pts + 10 pts slippage; SL-first;
  in-sample until 2026-06-24, out-of-sample after). Report: exports/backtest.json.
- Result (avg R per trade, after costs / without costs):
  RANGE_FADE v1 −0.24 / +0.04 (3668 trades, win 16%); RANGE_FADE v2 −0.25 / −0.00 (1975);
  BREAKOUT −0.37 / −0.12 (2128); PULLBACK −0.15 / +0.00 (2379). Out-of-sample matches in-sample.
- Reading: as coded, none of the setups has an edge even before costs; costs (~65–75 pts per trade vs a median
  risk of ~280 pts on fades) add ≈ −0.25R. v2 filters halve the number of fades but do not improve them.
  M1 vs M5 fills give the same picture, so this is not a fill-model artifact.
- Caveat: the backtest cannot reproduce the LLM's discretion (cancels, early closes, reading structure), and
  its rules are my translation of the playbook. But the live sample (1W/7L) points the same way.
- Implication: tuning filters on these setups is unlikely to help. Next research: setups with a pre-cost edge
  measurable in history (larger timeframe stops/targets so cost < 10% of risk, session/time effects,
  momentum vs mean reversion by regime), validated out-of-sample before they go into the playbook.

## 2026-11-24 21:40 — research round 1 (no new setup adopted)
- Event study (in-sample only, `python -m xau_agent.research`): no mean reversion; extensions from H1 EMA20 and
  H1 Donchian breakouts tended to continue; strongest effects were long-side and in late server hours (20–01),
  i.e. consistent with the 2025–26 bull run (price 3317 -> 4033 in-sample).
- Candidates (`python -m xau_agent.h1lab`), market entry at H1 close, exits in H1 ATR (1/2 @24h, 1.5/3 @48h),
  each compared with a same-direction "enter every hour" baseline to strip out drift:
  in-sample donchian20 and bias_flip beat their baselines by ~+0.1R (n 100–300, ~1.5–2 SE: weak).
- Out-of-sample (2026-06-24..11-24, price 4015 -> 3626, run once): neither survives. donchian20 buy −0.09R vs
  baseline −0.13R, sell +0.03..0.05R vs baseline +0.06..0.10R; bias_flip worse than baseline on both sides.
- Conclusion: the in-sample edge was mostly market drift. No simple M5–H1 technical rule tested so far has a
  robust edge after retail costs on XM GOLD. Not adopting anything into the playbook.
- Next options: (1) daily/H4 time-series momentum with multi-year history (best-documented effect, but it is
  swing trading, not scalping); (2) keep the live demo as a data-collection experiment only.

## 2026-11-24 — research round 2: D1/H4 swing trend-following (`python -m xau_agent.swing`)
- Data: XM GOLD D1 2001-06..2026-11 (6625 bars); H4 is real 4-hourly only from 2013-05 (earlier = daily).
- Canonical rules only (no parameter fitting): Donchian 20/10 and 55/20 (2x ATR20 stop), SMA 50/200 (3x ATR).
  Costs scaled to price from today's XM terms: spread 55 + slip 10 pts at 3620, swap long −69.5 / short +15.07
  pts per night (triple Wed). Control: BASE = enter whenever flat with the same exits (no signal).
- Best robust candidate: H4 DON20_10, long+short, 2013-06..2026-11: 603 trades (~43/yr), avg +0.128R,
  total +77R, 12/14 positive years, max drawdown 18R, worst losing streak 12, median hold 4 days.
  Signal beats the no-signal baseline on BOTH sides in BOTH sub-periods (long +0.185R vs +0.008R; short +0.064R
  vs −0.079R). Worst year 2018 (−15.7R).
- D1: Donchian long side beats baseline in 2 of 3 periods (DON55_20 long +0.77R/trade but only 56 trades);
  D1 shorts lose, worse than baseline. SMA 50/200: long OK, short negative; too few trades to trust.
- Reading: a modest, plausible trend-following edge exists at H4/D1 after costs (≈ +0.13R/trade; at 0.5% risk
  ≈ +2.9%/yr with ~9% max drawdown historically). Not adopted live yet: it is a different product (swing,
  multi-day holds, swap) from the M5 agent. Next step if approved: forward-test H4 DON20_10 on demo as a
  deterministic bot with the LLM as monitor/veto, and compare live fills with the backtest.

## 2026-11-24 21:55 — H4 swing bot forward test started on demo
- `python -m xau_agent.swing_bot`: H4 DON20_10 exactly as backtested (unit test checks live signals == backtest
  entries), broker-side stop 2x ATR20, no TP, channel exit, same-bar flip, missed exits caught up after downtime.
- Separate process (pid logs/swing.pid), magic 260513; excluded from the M5 agent's daily cap and setup stats.
  Account equity/daily P&L is shared, so swing P&L does affect the M5 risk engine's daily loss limit.
- No LLM veto during the forward test (it would make live results incomparable with the backtest).
- Risk: SWING_RISK_PCT=1.0 — at ~$9k equity the minimum 0.01 lot with a 2x H4 ATR stop (ATR ≈ $47 -> stop ≈ $94)
  already risks ~1%, so 0.5% would skip every trade.
- Expect ~1 trade per week, ~35% win rate, losing streaks up to ~12; judge after 30+ trades, not weeks.

## 2026-11-24 22:20 — auto-cancel runaway pending orders (user report)
- Case: 15:05 UTC PULLBACK sell_limit 3621.7 (SL 3627, TP 3611), Opus-approved. Price fell straight to 3589
  without a retest, i.e. through the TP, and the agent kept "keeping the pending order" each cycle; only the
  30-min expiry would have removed it.
- Fix (sweep every 10 s, no LLM): cancel when (a) price already reached the order's TP before it filled
  (`tp_passed`), or (b) price is further from the entry than MAX_PENDING_DISTANCE_POINTS (`too_far`).
  On restart it cancelled ticket 2386272660 as tp_passed (bid 3589.4).
- Note: the setup was directionally right (sell, all 5 TFs down) but the limit never filled — a missed trade,
  not a loss. Watch how often PULLBACK limits are skipped by fast moves.

## 2026-11-24 23:40 — economic calendar (news.py)
- Source: ForexFactory weekly JSON (free, unofficial; no actual values). Refreshed at start and every 6 h into
  table `news_events`; `news_fetch` events log success/failure. Gold-relevant = USD High, or USD Medium matching
  Fed/inflation/jobs/growth/PMI/confidence keywords.
- Risk engine: blocks new entries 15 min before to 15 min after USD High-impact events (NEWS_AUTO, NEWS_BLOCK_*).
  If the calendar is unavailable or >36 h old: blocks the 08:30 and 10:00 New York slots instead (DST-aware).
- Agent and Opus see `upcoming_news_3h`; playbook has a News section.
- First evidence: CB Consumer Confidence (Medium) 2026-11-24 15:00 UTC -> gold −$19 in 15 min, −$31 in 30 min
  (the large candle the user asked about). Medium events are shown, not blocked; revisit if more evidence.
- Next: Wed 25 Nov 13:30 UTC (20:30 Thai) Core PCE + Prelim GDP (High) -> blackout 20:15–20:45 Thai.
- Swing bot does not use the calendar (forward test must match the backtest).

## 2026-11-24 23:50 — DEV MODE caps (user: "let it keep entering for optimization and data analysis")
- At 23:40 the agent had used 10/10 real fills for the UTC day (count verified) and stopped trading.
- .env (demo only): MAX_TRADES_PER_DAY 10 -> 100, MAX_DAILY_LOSS_PCT 2 -> 5. Unchanged: 0.5% risk/trade,
  MAX_LOT 0.01, one exposure at a time, HTF gate, news blackout, Opus review. Backup .env.bak-dev-2350.
- Must restore 10 / 2 (or re-decide) before any real account.
- Ops gotcha: the main process loads .env into os.environ at start and child processes (Claude Code -> CLI)
  inherit it; python-dotenv does not override existing vars. So .env edits need a restart of `xau_agent.main`.
  Verified after restart: 23:55 cycle sees max_trades_per_day 100, daily loss 5%.

## 2026-11-25 00:20 — monitoring dashboard (user request)
- `python -m xau_agent.dashboard --watch` rewrites a self-contained `dashboard.html` every 30 s (gitignored):
  pipeline stage cards, cycle timeline with per-cycle tool calls/results/reasoning, Opus verdicts, risk
  rejections, positions/pending, setup stats, news, system events. Read-only. Note: Claude Code's thinking
  blocks arrive empty in stream-json, so the dashboard shows tool calls + final reasoning, not hidden thinking.

## 2026-11-25 00:35 — live "current stage" + keep-awake
- status.py: main loop, CLI tools (live cycles only) and swing bot publish the current stage to
  logs/status_<m5|swing>.json and status_<m5|swing>.js; dashboard.html reloads the .js every 2 s and draws a
  flow (wait M5 -> market -> account/news -> Sonnet -> risk -> Opus -> MT5 -> done) with the active node
  pulsing, blocked/error nodes coloured, countdown to the next candle, and a live transition feed.
  Verified on the 00:35 cycle: market -> account -> agent -> done (no_trade) in ~10 s.
- Bots call SetThreadExecutionState so Windows does not idle-sleep while they run (no settings changed;
  released when the process exits). Lid close / manual sleep still sleeps.
- Overnight plan (user asleep): hourly reviews until trading hours end (03:00 Thai), then morning check.
  Optimisation scope tonight = behaviour/bug fixes and pre-declared rules only; no strategy tuning on M5 setups
  (backtest showed no edge; tuning them would be overfitting noise).

## 2026-11-25 03:10 — overnight review: enforce the playbook's minimum stop
- 01:40–03:00 Thai: 4 sell orders, all Opus-approved, all trend-aligned (5/5 TF down):
  BREAKOUT sell stop 3557.6 -> SL −4.69; PULLBACK sell limit 3561.1 (SL $2.90 away) -> SL −2.90;
  two more PULLBACK sell limits with SL $2.7–2.9 away -> cancelled `tp_passed` (price fell without retest).
- Behaviour bug: PULLBACK stops were ~0.35x M5 ATR, below the playbook's "stops tighter than ~0.6x M5 ATR get
  hit by noise" — and Opus approved them anyway. A written rule that is not enforced is not a rule.
- Fix: risk engine rejects SL < MIN_SL_ATR (0.6) x M5 ATR14 (computed from 200 M5 bars at proposal time);
  account state shows `min_sl_atr_m5`. Not a parameter tune: it enforces an existing playbook rule.
- Night totals: equity 9010.68 -> 9003.09. M5 setups since start remain negative, as the backtest predicts.

## 2026-11-25 16:10 — range/chop filter study (xau_agent/regime.py): REJECTED, nothing changed live
- Motivation: 5 trend-following sells in a row hit SL while M15/M30/H1 EMAs said "down" but price chopped in a
  3535–3560 box. Hypothesis: a filter that detects "trend has stalled" would remove these losers.
- Pre-declared filters (fixed params, no sweep): F1 PROGRESS = new 2 h extreme vs the 2 h before (sell: lower
  low); F2 EFFICIENCY = Kaufman ER over 24 M5 closes >= 0.30; F1+F2. Pre-declared rule: adopt only if kept
  trades beat unfiltered avg R in-sample AND out-of-sample for both setups, removed trades are worse than kept
  ones, and >= 100 kept trades. Backtest: M5 2025-06..2026-11, 70/30 split, spread>=55 + 10 slippage.
- Results (avg R, full / IS / OOS):
  BREAKOUT none −0.371/−0.361/−0.398; F1 keep −0.369/−0.354/−0.407; F2 keep −0.395/−0.369/−0.451;
           F1+F2 keep −0.381/−0.357/−0.433. Removed trades were BETTER than kept (F2 removed −0.332).
  PULLBACK none −0.147/−0.164/−0.107; F1 keep −0.163/−0.177/−0.129; F2 keep −0.160/−0.211/−0.015 (n 401);
           F1+F2 keep −0.154/−0.206/−0.026 (n 329). PULLBACK F2 improves OOS only, worse IS: fails the rule.
- Verdict: no filter passes. "Stalled trend" does not separate winners from losers in these setups; the live
  losing streak is ordinary variance of setups with negative expectancy (no-cost avg R ≈ 0). Not adopted.
  Reconfirms: the M5 setups need a different source of edge, not more filters. H4 swing stays the main test.

## 2026-11-25 16:45 — direction change: proven rules trade, M5 agent only observes; BTCUSD swing added
- First git commit of the project (6f6acc3).
- M5 agent restarted with MODE=advisory (the .env value; it had been launched with a MODE=demo override). It keeps
  analysing every M5 bar and logging, but sends no orders: the M5 setups have no edge (backtest + regime study),
  so more live losses would only re-prove that. The H4 swing bot(s) remain on DEMO.
- Multi-symbol test of the unchanged H4 DON20_10 rule (xau_agent/multi.py, rules and pass criteria declared
  before running: n>=100, avg R>=+0.05, >0 in both halves, beats the enter-whenever-flat baseline). Real-H4
  periods only; per-symbol spread from history, slippage 20% of spread, today's XM swaps.
  avg R (n): GOLD +0.135 (603) PASS | BTCUSD +0.459 (527) PASS | SILVER +0.054 (1st half −0.058) | GER40 −0.016 |
  JP225 −0.016 | OIL −0.001 | EURUSD −0.071 | GBPUSD −0.066 | USDJPY −0.059 | USDCAD −0.074 | US100 −0.071 |
  US30 −0.087 | AUDUSD −0.128 | UK100 −0.163 | US500 −0.168. Indices: longs positive, shorts strongly negative.
  2 of 15 pass where ~1 is expected by luck, so passes are forward-test candidates, not proof.
- BTCUSD checks: positive in 7/10 years (2021 0.00, 2022 −0.10, 2025 −0.23); without the 5 biggest winners
  avg R +0.19; counting swap 7 days/week changes nothing. Money PF 1.01 only because price rose 1k -> 59k
  (late trades dominate in USD). Spread is large (8.5 bp) and already in the test.
- swing_bot: per-symbol namespace (cycle ids "swing-<SYM>-<bar>", status component "swing_<sym>") so a second
  instance cannot collide with GOLD; GOLD keeps its original ids. Report sizes risk from the symbol spec.
- Started BTCUSD swing bot on DEMO: SYMBOL=BTCUSD SWING_MAGIC=260514 SWING_MAX_SPREAD_POINTS=10000
  (pid logs/swing_btcusd.pid), risk 1%/trade, max 0.05 lot. Dashboard shows a BTCUSD swing panel.

## 2026-11-25 17:05 — how much evidence do the swing results carry? (correction)
- R per trade is very dispersed (sd 2.0R GOLD, 3.8R BTCUSD). GOLD H4 DON20_10: mean +0.130R over 603 trades,
  t = 1.59 — NOT statistically significant on its own (needs ~950 trades for t=2). Earlier wording "edge" was too
  strong: it is weak evidence, supported by consistency (12/14 years) and beating the baseline. BTCUSD: t = 2.78
  over 527 trades, but chosen among 15 symbols, so discount it.
- Expected forward-test noise: 1-year windows (45 trades) were negative 33% (GOLD) / 26% (BTC) of the time in the
  backtest; worst 1-year −15R / −18R; longest losing streaks 12 / 14 trades. A losing first year is NOT evidence
  against the rule. Forward test purpose = verify execution matches the backtest (fills, spread, slippage, missed
  signals), not to prove edge.

## 2026-11-25 17:25 — H1 version of the swing rule: FAIL, H4 only
- Pre-declared (multi.py header): same DON20_10 rule on H1, same cost model, real-H1 data only; pass needs the H4
  criteria plus avg R > 0 on the last 30% of trades and t >= 2.
- GOLD H1 (2018–2026): 1625 trades (183/yr), avg R +0.019, t 0.41, halves −0.022 / +0.059, last 30% +0.112,
  buy +0.091 / sell −0.059, PF 1.06 -> FAIL (fails avg R and t).
- BTCUSD H1 (2020–2026): 1586 trades (230/yr), avg R +0.021, t 0.37, halves +0.092 / −0.021, last 30% −0.085,
  PF 0.88 -> FAIL.
- Reading: more trades but the per-trade edge falls to ~0, so evidence does not accumulate faster. Same pattern
  as M5/H1 earlier: the shorter the horizon, the smaller the edge relative to costs and noise.
- For transparency, H4 against the stricter H1 bar: GOLD t 1.67 (fails t>=2), BTCUSD last 30% +0.012 (barely
  positive). Both stay as forward-test candidates under the original H4 criteria, with weak evidence.

## 2026-11-25 17:55 — shadow AI review of swing trades (logged only)
- xau_agent/shadow.py: after every swing entry the bot launches a detached `python -m xau_agent.shadow --cycle
  <id>`; Opus gets the trade, D1/H4 bars + trend, the high/medium-impact calendar (−24 h..+72 h) and may run up
  to 4 WebSearches; it answers take / half / skip + confidence, drivers, risks. Stored as event `shadow_review`
  and logs/shadow/<cycle>.json (full prompt + answer). The trade is never changed: the forward test must
  measure the rule, and the AI earns a vote only if `--report` shows "follow AI" R clearly above rule R.
- Sandbox for web text: Claude runs with --restricted --strict-mcp-config --tools WebSearch, cwd = empty folder,
  no API key (plan billing). It cannot read files (.env), run commands or fetch pages.
- Verified: dry review of GOLD sell -> "take", confidence 55, flagged today's US PCE 13:30 UTC as the main risk;
  21 s, 3 turns. Detached launch verified. Swing bots (GOLD, BTCUSD) restarted with the hook. 83 tests pass.
- Decision rule fixed now: do not judge before ~30 closed shadowed trades; compare follow_ai_R vs rule_R and
  avg R of "skip" vs "take".

## 2026-11-25 18:45 — chart check + intraday technique suite (xau_agent/techniques.py): no technique passes
- Data check: rendered the bot's own MT5 bars (M5/H1/H4) to images and read them; they show the move the bot
  reported (H4 downtrend 4150 -> 3510, testing the 3502.63 low; M5 range 3515–3540 today). Independent source:
  Kitco 18 Nov 3724.90 matches the H4 bars that day. The bot sees the real market.
- Research (web): gold "rises in Asia, falls later" (Blose et al. 2018; CBS thesis) — academic support, but a
  practitioner tick backtest found costs eat it; intraday momentum (Gao et al.) — mixed evidence for gold.
- Suite, pre-declared in the module header (13 canonical techniques, no tuning; pass = n>=200, IS and OOS
  avg R > 0, t >= 2). M5 2025-06..2026-11, ~368 trading days, real costs. One bug fixed before judging:
  ASIA_LONG entered at a 01:00 close that rarely exists (XM opens 01:00) -> first bar close 01:05.
  avg R after costs (n, trades/day, no-cost avg R):
  RSI2 −0.129 (8338, 22.6/d, −0.005) | RSI14 −0.131 (2455, 6.7, −0.019) | BB20 −0.123 (5619, 15.3, −0.011) |
  STOCH −0.109 (5676, 15.4, +0.013) | EMA9_21 −0.127 (3418, 9.3, −0.008) | MACD −0.139 (4731, 12.8, +0.007) |
  DON20 −0.145 (5071, 13.8, −0.027) | ORB_LON −0.201 (359, 1.0, −0.026) | ORB_NY −0.143 (344, 0.9, −0.063) |
  ASIA_BRK −0.103 (187, 0.5, +0.038) | ASIA_LONG +0.009 (362; IS +0.122, OOS −0.253; no-cost +0.129) |
  DAY_SHORT +0.029 (362; IS −0.007, OOS +0.114; t 0.27; no-cost +0.096) | NY_MOM −0.039 (362, no-cost +0.005).
- Reading: every indicator technique has ~0 R BEFORE costs (they do not predict 5–60 min moves on this data);
  costs are ~0.12 R per trade, so the more trades, the larger the loss. Only the time-of-day legs show a pre-cost
  tilt (+0.10–0.13 R) and it is unstable between halves. High-frequency trading on this account has no evidence
  of edge; frequency multiplies costs, not profit.

## 2026-11-25 19:10 — focus: H4 swing + AI (user decision); forward-test bookkeeping
- Direction chosen by the user: H4 swing bots (GOLD, BTCUSD) on demo + shadow AI; M5 agent stays advisory.
- swing_bot: records the last H4 bar seen online (logs/seen_<component>.txt). After a restart or PC sleep, entry
  signals on bars that closed while offline are logged as event `swing_missed` (+ notify), so forward results
  can be compared with the backtest honestly (missed trades are not silently dropped). Checked: no signal fired
  on either symbol since the bots started (incl. today's 11:26–15:39 sleep).
- Dashboard: "Swing H4 + Shadow AI" panel — each swing trade (symbol, side, entry/SL, open or net result) next to
  the AI verdict/confidence/reason, plus the list of missed signals. Verified with sample rows on a copy.
- 84 tests pass. Swing bots and dashboard restarted.

## 2026-11-25 17:40 — news-reaction continuation (techniques.py NEWS*): FAIL; reversal noted as a NEW hypothesis
- Pre-declared: after US releases at 08:30 / 10:00 / 14:00 NY, if the first 15 min move > 1.5x ATR14, trade WITH
  it, stop 3.5x ATR, exit after 2 h (also 4 h for 08:30). Pass needs n>=60, IS & OOS > 0, t>=2.
- Results (avg R after costs, n, IS / OOS, t, no-cost):
  NEWS0830_2H −0.134 (141; −0.022 / −0.325; −1.36; −0.105) | NEWS0830_4H −0.075 (141; +0.036 / −0.263; −0.61; −0.046)
  NEWS1000_2H −0.089 (83; −0.107 / −0.031; −1.14; −0.058) | NEWS1400_2H −0.290 (47; −0.543 / +0.053; −2.56; −0.259)
- Continuation is rejected. Every variant is negative even before costs, i.e. first news moves in gold tended to
  partly REVERSE over the next hours, most at 14:00 NY (Fed statements/minutes). That is a hypothesis formed after
  seeing the data (snooping), so it is NOT adopted. To test it fairly it needs new data: record each live news
  reaction from now on and check the fade only on events after 2026-11-25.

## 2026-11-25 20:00 — bots were being suspended by Modern Standby; keep-screen-on helper
- Finding: M5 cycles stopped 17:36–17:51 and 19:36–19:51 (one cycle "took" 26 min). Kernel-Power 506/507 show the
  PC entering S0 low-power idle (Modern Standby) when the screen turns off (display timeout 3 min), which suspends
  desktop programs. ES_SYSTEM_REQUIRED (bots) and the app's keep-awake do not prevent it. Laptop was on battery.
- xau_agent/awake.py: holds ES_DISPLAY_REQUIRED|ES_SYSTEM_REQUIRED (screen stays on, no Windows setting changed;
  stop the process to undo; lid close still sleeps). Started with the user's approval (pid logs/awake.pid).

## 2026-11-25 20:35 — round 2: symbols similar to GOLD/BTC on H4 DON20_10 (multi.py --strict): none pass
- Pre-declared (multi.py header): XPTUSD, XPDUSD, BRENTCash, NGASCash and 10 liquid crypto majors; same rule and
  cost model; stricter bar for the larger test count (H4 criteria + last 30% > 0 + t >= 2).
- avg R (n, t, spread bp): XPT −0.332 (330, −4.31, 40) | XPD −0.416 (298, −4.83, 79) | BRENT −0.018 (465, −0.23, 3) |
  NGAS −0.280 (515, −4.61, 50) | ETH +0.252 (577, +2.15, 30; 2nd half −0.040, last 30% −0.061) |
  XRP +0.390 (570, +0.66; all from 2017–2021, 2nd half −0.355) | LTC −1.358 (296 bp spread) | BCH −0.706 |
  SOL −0.213 | ADA −0.496 | DOGE −0.816 | LINK −0.045 | DOT −0.139 | AVAX −0.040.
- Reading: XM spreads on altcoins (17–300 bp per trade) are far too wide for this rule; platinum/palladium/gas
  lose clearly; ETH is the only near-miss and fails the recent-period checks. No symbol added. GOLD + BTCUSD stay.

## 2026-11-26 09:15 — results 25 Nov (Thai day) and first swing trades
- Equity 9010.68 (25 Nov 00:00 Thai) -> 8814.85 (26 Nov 09:10) = −195.83.
- M5 agent (demo until 16:25, then advisory): 5 trades, all sells, all SL: −4.69, −2.90, −6.57, −6.45, −4.10 = −24.71.
- Swing H4, first forward trades (both entered 21:00 Thai after US PCE, 7 s after the bar closed):
  GOLD sell 0.01 @ 3476.24, broker SL 3569.49 hit 03:13 Thai -> −93.25 (−1.0R; gold bounced ~3% overnight).
  BTCUSD sell 0.03 @ 58034.05, broker SL 60613.75 hit 07:10 Thai -> −77.39 (−1.0R).
  Shadow AI said "take" on both (60% / 55%); its stated main risk for GOLD was exactly a >2.6% short-covering
  bounce to the stop. 2 trades = no evidence either way (backtest: ~2/3 of trades lose; streaks of 12–14 occur).
- Ops: PC entered standby at 00:08 Thai and hibernated 00:24 ("Hibernate from Sleep - Fixed Timeout") despite the
  awake helper -> bots offline 00:08–09:04 (lid close or manual sleep likely; awake cannot block those). Both
  exits were broker-side stops, so nothing was missed; no missed entry signals were logged after wake.
