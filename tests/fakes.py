"""In-memory broker for tests (no MT5 needed)."""
from __future__ import annotations

from datetime import datetime, timezone

from xau_agent.broker import AccountState, OrderResult, PendingOrder, Position, Quote, SymbolSpec

GOLD_SPEC = SymbolSpec(
    point=0.01, digits=2, tick_size=0.01, tick_value=1.0,  # 1 lot = 100 oz -> $1 per 0.01 move
    volume_min=0.01, volume_max=50, volume_step=0.01, stops_level=0,
)


class FakeBroker:
    def __init__(self, bid=2650.00, ask=2650.20, equity=1000.0, is_demo=True):
        self._q = Quote(bid=bid, ask=ask, time=datetime.now(timezone.utc), server_epoch=1_700_000_000)
        self.deals: dict[int, dict] = {}
        self._pending: list[PendingOrder] = []
        self.cancelled: list[int] = []
        self.equity = equity
        self.is_demo = is_demo
        self._positions: list[Position] = []
        self.sent: list[tuple] = []
        self.checked: list[tuple] = []

    def spec(self):
        return GOLD_SPEC

    def quote(self):
        return self._q

    def account(self):
        return AccountState(1, self.is_demo, "demo" if self.is_demo else "real", "USD",
                            self.equity, self.equity, self.equity, 500)

    def positions(self):
        return list(self._positions)

    def bars(self, timeframe, count):
        base = 1_700_000_000
        return [
            {"time": base + i * 60, "open": 2650 + i * 0.1, "high": 2650.5 + i * 0.1,
             "low": 2649.5 + i * 0.1, "close": 2650.2 + i * 0.1, "tick_volume": 100}
            for i in range(count)
        ]

    def last_bar_time(self, timeframe):
        return 0

    def check_order(self, side, lot, sl, tp):
        self.checked.append((side, lot, sl, tp))
        return OrderResult(ok=True, retcode=0, comment="Done")

    def send_order(self, side, lot, sl, tp, comment):
        self.sent.append((side, lot, sl, tp))
        self._positions.append(Position(len(self.sent), side, lot, self._q.ask, sl, tp, 0.0, 260512))
        return OrderResult(ok=True, retcode=10009, comment="Request executed", ticket=len(self.sent),
                           price=self._q.ask, sent=True)

    def shutdown(self):
        pass

    def deal_history(self, position_id):
        return self.deals.get(position_id)

    def close_position(self, ticket):
        self._positions = [p for p in self._positions if p.ticket != ticket]
        return OrderResult(ok=True, retcode=10009, comment="closed", ticket=ticket, sent=True)

    def pending_orders(self):
        return list(self._pending)

    def check_pending(self, order_type, lot, price, sl, tp):
        return OrderResult(ok=True, retcode=0, comment="Done")

    def place_pending(self, order_type, lot, price, sl, tp, comment, expires_epoch=None):
        self.last_pending_expiry = expires_epoch
        ticket = 1000 + len(self._pending) + len(self.cancelled)
        self._pending.append(PendingOrder(ticket, order_type, lot, price, sl, tp, 260512))
        return OrderResult(ok=True, retcode=10008, comment="Request placed", ticket=ticket, price=price, sent=True)

    def cancel_pending(self, ticket):
        self._pending = [o for o in self._pending if o.ticket != ticket]
        self.cancelled.append(ticket)
        return OrderResult(ok=True, retcode=10009, comment="cancelled", ticket=ticket, sent=True)
