"""MT5 gateway. The only module that talks to the broker."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from . import indicators


@dataclass
class SymbolSpec:
    point: float
    digits: int
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int


@dataclass
class Quote:
    bid: float
    ask: float
    time: datetime
    server_epoch: int = 0  # raw MT5 tick time (broker server clock, same scale as bar times)

    def spread_points(self, point: float) -> float:
        return round((self.ask - self.bid) / point, 1)


@dataclass
class Position:
    ticket: int
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    magic: int


@dataclass
class PendingOrder:
    ticket: int
    order_type: str
    volume: float
    price: float
    sl: float
    tp: float
    magic: int


@dataclass
class AccountState:
    login: int
    is_demo: bool
    trade_mode: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int


@dataclass
class OrderResult:
    ok: bool
    retcode: int | None
    comment: str
    ticket: int | None = None
    price: float | None = None
    sent: bool = False


class Broker(Protocol):
    def spec(self) -> SymbolSpec: ...
    def quote(self) -> Quote: ...
    def account(self) -> AccountState: ...
    def positions(self) -> list[Position]: ...
    def bars(self, timeframe: str, count: int) -> list[dict[str, Any]]: ...
    def last_bar_time(self, timeframe: str) -> int: ...
    def check_order(self, side: str, lot: float, sl: float, tp: float) -> OrderResult: ...
    def send_order(self, side: str, lot: float, sl: float, tp: float, comment: str) -> OrderResult: ...
    def close_position(self, ticket: int) -> OrderResult: ...
    def deal_history(self, position_id: int) -> dict[str, Any] | None: ...
    def pending_orders(self) -> list[PendingOrder]: ...
    def check_pending(self, order_type: str, lot: float, price: float, sl: float, tp: float) -> OrderResult: ...
    def place_pending(self, order_type: str, lot: float, price: float, sl: float, tp: float,
                      comment: str, expires_epoch: int | None = None) -> OrderResult: ...
    def cancel_pending(self, ticket: int) -> OrderResult: ...


def safe_comment(text: str) -> str:
    """MT5 rejects some comments ('Invalid comment argument'); keep [A-Za-z0-9_], max 25 chars."""
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in text)
    return cleaned[:25] or "xau"


TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


class MT5Broker:
    def __init__(self, symbol: str, magic: int, login: int | None, password: str, server: str, path: str = ""):
        import MetaTrader5 as mt5  # Windows-only package

        self.mt5 = mt5
        self.symbol = symbol
        self.magic = magic
        kwargs: dict[str, Any] = {}
        if path:
            kwargs["path"] = path
        if login:
            kwargs.update(login=login, password=password, server=server)
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not mt5.symbol_select(symbol, True):
            names = [s.name for s in (mt5.symbols_get() or []) if "XAU" in s.name or "GOLD" in s.name.upper()]
            mt5.shutdown()
            raise RuntimeError(f"Symbol {symbol!r} not found. Gold-like symbols on this account: {names}")

    def shutdown(self) -> None:
        self.mt5.shutdown()

    # ---------- read ----------
    def spec(self) -> SymbolSpec:
        info = self.mt5.symbol_info(self.symbol)
        return SymbolSpec(
            point=info.point,
            digits=info.digits,
            tick_size=info.trade_tick_size,
            tick_value=info.trade_tick_value,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            stops_level=info.trade_stops_level,
        )

    def quote(self) -> Quote:
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None:
            raise RuntimeError(f"No tick for {self.symbol}: {self.mt5.last_error()}")
        return Quote(
            bid=tick.bid, ask=tick.ask,
            time=datetime.fromtimestamp(tick.time, tz=timezone.utc), server_epoch=int(tick.time),
        )

    def account(self) -> AccountState:
        a = self.mt5.account_info()
        if a is None:
            raise RuntimeError(f"account_info failed: {self.mt5.last_error()}")
        modes = {
            self.mt5.ACCOUNT_TRADE_MODE_DEMO: "demo",
            self.mt5.ACCOUNT_TRADE_MODE_CONTEST: "contest",
            self.mt5.ACCOUNT_TRADE_MODE_REAL: "real",
        }
        mode = modes.get(a.trade_mode, "unknown")
        return AccountState(
            login=a.login,
            is_demo=mode == "demo",
            trade_mode=mode,
            currency=a.currency,
            balance=a.balance,
            equity=a.equity,
            margin_free=a.margin_free,
            leverage=a.leverage,
        )

    def positions(self) -> list[Position]:
        rows = self.mt5.positions_get(symbol=self.symbol) or ()
        return [
            Position(
                ticket=p.ticket,
                side="buy" if p.type == self.mt5.POSITION_TYPE_BUY else "sell",
                volume=p.volume,
                price_open=p.price_open,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit,
                magic=p.magic,
            )
            for p in rows
        ]

    def _tf(self, timeframe: str) -> int:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe}")
        return getattr(self.mt5, f"TIMEFRAME_{timeframe}")

    def bars(self, timeframe: str, count: int) -> list[dict[str, Any]]:
        rates = self.mt5.copy_rates_from_pos(self.symbol, self._tf(timeframe), 0, count)
        if rates is None:
            raise RuntimeError(f"copy_rates failed: {self.mt5.last_error()}")
        return [
            {
                "time": int(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
            }
            for r in rates
        ]

    def last_bar_time(self, timeframe: str) -> int:
        rates = self.mt5.copy_rates_from_pos(self.symbol, self._tf(timeframe), 0, 1)
        return int(rates[0]["time"]) if rates is not None and len(rates) else 0

    # ---------- write ----------
    def _filling(self) -> int:
        mode = self.mt5.symbol_info(self.symbol).filling_mode
        if mode & 1:  # SYMBOL_FILLING_FOK
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:  # SYMBOL_FILLING_IOC
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def _request(self, side: str, lot: float, sl: float, tp: float, comment: str) -> dict[str, Any]:
        q = self.quote()
        return {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lot,
            "type": self.mt5.ORDER_TYPE_BUY if side == "buy" else self.mt5.ORDER_TYPE_SELL,
            "price": q.ask if side == "buy" else q.bid,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": self.magic,
            "comment": safe_comment(comment),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(),
        }

    def check_order(self, side: str, lot: float, sl: float, tp: float) -> OrderResult:
        """Validate with the broker without sending (order_check)."""
        res = self.mt5.order_check(self._request(side, lot, sl, tp, "xau-agent check"))
        if res is None:
            return OrderResult(ok=False, retcode=None, comment=str(self.mt5.last_error()))
        return OrderResult(ok=res.retcode == 0, retcode=res.retcode, comment=res.comment)

    def send_order(self, side: str, lot: float, sl: float, tp: float, comment: str) -> OrderResult:
        req = self._request(side, lot, sl, tp, comment)
        res = self.mt5.order_send(req)
        if res is None:
            return OrderResult(ok=False, retcode=None, comment=str(self.mt5.last_error()), sent=True)
        ok = res.retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED)
        return OrderResult(ok=ok, retcode=res.retcode, comment=res.comment, ticket=res.order, price=res.price, sent=True)

    def close_position(self, ticket: int) -> OrderResult:
        rows = self.mt5.positions_get(ticket=ticket)
        if not rows:
            return OrderResult(ok=False, retcode=None, comment=f"position {ticket} not found")
        p = rows[0]
        if p.magic != self.magic:
            return OrderResult(ok=False, retcode=None, comment="position not opened by this agent")
        side = "sell" if p.type == self.mt5.POSITION_TYPE_BUY else "buy"
        req = self._request(side, p.volume, 0.0, 0.0, "xau-agent close")
        req["position"] = ticket
        res = self.mt5.order_send(req)
        if res is None:
            return OrderResult(ok=False, retcode=None, comment=str(self.mt5.last_error()), sent=True)
        ok = res.retcode == self.mt5.TRADE_RETCODE_DONE
        return OrderResult(ok=ok, retcode=res.retcode, comment=res.comment, ticket=ticket, price=res.price, sent=True)

    # ---------- pending orders ----------
    def _pending_types(self) -> dict[str, int]:
        m = self.mt5
        return {"buy_stop": m.ORDER_TYPE_BUY_STOP, "buy_limit": m.ORDER_TYPE_BUY_LIMIT,
                "sell_stop": m.ORDER_TYPE_SELL_STOP, "sell_limit": m.ORDER_TYPE_SELL_LIMIT}

    def pending_orders(self) -> list[PendingOrder]:
        names = {v: k for k, v in self._pending_types().items()}
        rows = self.mt5.orders_get(symbol=self.symbol) or ()
        return [
            PendingOrder(ticket=o.ticket, order_type=names.get(o.type, str(o.type)), volume=o.volume_current,
                         price=o.price_open, sl=o.sl, tp=o.tp, magic=o.magic)
            for o in rows if o.type in names
        ]

    def _pending_request(self, order_type: str, lot: float, price: float, sl: float, tp: float,
                         comment: str, expires_epoch: int | None = None) -> dict[str, Any]:
        info = self.mt5.symbol_info(self.symbol)
        digits = info.digits
        # Broker-side expiry (server time) when the symbol allows it, so a pending order still expires
        # if this PC is off. Our own sweep stays as a backup.
        specified = bool(expires_epoch) and bool(info.expiration_mode & 4)  # SYMBOL_EXPIRATION_SPECIFIED
        req = {
            "action": self.mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": lot,
            "type": self._pending_types()[order_type],
            "price": round(price, digits),
            "sl": round(sl, digits),
            "tp": round(tp, digits),
            "magic": self.magic,
            "comment": safe_comment(comment),
            "type_time": self.mt5.ORDER_TIME_SPECIFIED if specified else self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_RETURN,
        }
        if specified:
            # MT5 floors expiration to the minute; round up so the order lives at least as long as asked
            req["expiration"] = -(-int(expires_epoch) // 60) * 60
        return req

    def check_pending(self, order_type: str, lot: float, price: float, sl: float, tp: float) -> OrderResult:
        res = self.mt5.order_check(self._pending_request(order_type, lot, price, sl, tp, "xau check"))
        if res is None:
            return OrderResult(ok=False, retcode=None, comment=str(self.mt5.last_error()))
        return OrderResult(ok=res.retcode == 0, retcode=res.retcode, comment=res.comment)

    def place_pending(self, order_type: str, lot: float, price: float, sl: float, tp: float,
                      comment: str, expires_epoch: int | None = None) -> OrderResult:
        res = self.mt5.order_send(self._pending_request(order_type, lot, price, sl, tp, comment, expires_epoch))
        if res is None:
            return OrderResult(ok=False, retcode=None, comment=str(self.mt5.last_error()), sent=True)
        ok = res.retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED)
        return OrderResult(ok=ok, retcode=res.retcode, comment=res.comment, ticket=res.order, price=price, sent=True)

    def cancel_pending(self, ticket: int) -> OrderResult:
        res = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": ticket})
        if res is None:
            return OrderResult(ok=False, retcode=None, comment=str(self.mt5.last_error()), sent=True)
        ok = res.retcode == self.mt5.TRADE_RETCODE_DONE
        return OrderResult(ok=ok, retcode=res.retcode, comment=res.comment, ticket=ticket, sent=True)

    def deal_history(self, position_id: int) -> dict[str, Any] | None:
        """Realized result of a position from MT5 deal history. None if no deals found."""
        mt5 = self.mt5
        deals = mt5.history_deals_get(position=position_id)
        if not deals:
            return None
        ins = [d for d in deals if d.entry == mt5.DEAL_ENTRY_IN]
        outs = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, getattr(mt5, "DEAL_ENTRY_OUT_BY", -1))]
        if not outs:
            return {"closed": False}
        reasons = {
            getattr(mt5, "DEAL_REASON_SL", -10): "sl",
            getattr(mt5, "DEAL_REASON_TP", -11): "tp",
            getattr(mt5, "DEAL_REASON_SO", -12): "stop_out",
            getattr(mt5, "DEAL_REASON_EXPERT", -13): "expert",
            getattr(mt5, "DEAL_REASON_CLIENT", -14): "manual",
        }
        net = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        return {
            "closed": True,
            "net_profit": round(net, 2),
            "entry_price": ins[0].price if ins else None,
            "exit_price": outs[-1].price,
            "exit_reason": reasons.get(outs[-1].reason, str(outs[-1].reason)),
            "open_server_epoch": int(ins[0].time) if ins else None,
            "close_server_epoch": int(outs[-1].time),
            "volume": sum(d.volume for d in ins),
        }


def build_snapshot(broker: Broker, timeframes: dict[str, int]) -> dict[str, Any]:
    out = _build_snapshot(broker, timeframes)
    from .mtf import summarize  # local import: mtf imports indicators only
    out["mtf_alignment"] = summarize({tf: v.get("trend") for tf, v in out["timeframes"].items()})
    return out


def _build_snapshot(broker: Broker, timeframes: dict[str, int]) -> dict[str, Any]:
    """Compact, numeric market view for the agent. timeframes = {"M5": 40, ...}."""
    spec = broker.spec()
    q = broker.quote()
    out: dict[str, Any] = {
        "quote": {
            "bid": q.bid,
            "ask": q.ask,
            "spread_points": q.spread_points(spec.point),
            "server_time": datetime.fromtimestamp(q.server_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        },
        "symbol_spec": {"point": spec.point, "digits": spec.digits, "stops_level_points": spec.stops_level},
        "note": "Bar times are broker server time. The last bar of each timeframe is still forming.",
        "timeframes": {},
    }
    for tf, count in timeframes.items():
        rows = broker.bars(tf, max(count, 200))  # enough history for EMA50 to converge
        closes = [r["close"] for r in rows]
        highs = [r["high"] for r in rows]
        lows = [r["low"] for r in rows]
        recent = rows[-count:]
        d = spec.digits
        out["timeframes"][tf] = {
            "ema20": _r(indicators.ema(closes, 20), d),
            "ema50": _r(indicators.ema(closes, 50), d),
            "atr14": _r(indicators.atr(highs, lows, closes, 14), d),
            "rsi14": _r(indicators.rsi(closes, 14), 1),
            **(indicators.trend(highs, lows, closes) or {"trend": None}),
            "range_high": max(r["high"] for r in recent),
            "range_low": min(r["low"] for r in recent),
            # [server_time, o, h, l, c, tick_volume]
            "bars": [
                [datetime.fromtimestamp(r["time"], tz=timezone.utc).strftime("%m-%d %H:%M"),
                 r["open"], r["high"], r["low"], r["close"], r["tick_volume"]]
                for r in recent
            ],
        }
    return out


def _r(v: float | None, digits: int) -> float | None:
    return None if v is None else round(v, digits)
