"""
Round 5 Trader — Batch 4

Changes from Batch 3
────────────────────
1. Pebbles    — Complete rewrite using teammate's framework:
                • basket_center = (sum_bids + sum_asks) / 2 each tick
                • Volume-weighted rolling mean of basket_center (window 500)
                  Weight = total L1 depth across all 5 legs that tick
                • Blended fair = 90% VW-rolling-mean + 10% structural 50k anchor
                  (50k is a real structural pattern, not hardcoding — the gap
                  between rolling mean and 50k is measured and partially corrected)
                • Z-score = (basket_center − blended_fair) / rolling_std
                • BUY basket:  z < −ENTRY_Z  AND  (blended_fair − sum_asks) ≥ MIN_EDGE
                • SELL basket: z > +ENTRY_Z  AND  (sum_bids − blended_fair) ≥ MIN_EDGE
                • Market-taking only (aggressive fills, no resting orders)
                • Symmetric: both long and short basket positions allowed (±10 each leg)

2. Microchips — Reverted to aggressive exits. Passive exit was net −6k.
                Removed: exiting flag, exit_ticks, CHIP_EXIT_TIMEOUT.
                Exit trigger → immediate _go_to(0) crossing the spread.

3. Snackpacks — Z-window 500 → 1000 (500 was too short; captured trends as MR).
                CHOC removed from traded legs — unreliable short, keeps causing losses.
                CHOC mid still drives the signal (z-score computation unchanged).
                Trade on HIGH_Z_SELL: VAN −10, RASP +10.  CHOC position = 0.

Unchanged: UV Visors, Sleep Pods, Panels, EOD flatten, position reconciliation.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Optional
import jsonpickle
import math
import statistics


class Trader:

    # ── 1. Pebbles ────────────────────────────────────────────────────────────
    PEBBLES = [
        "PEBBLES_XS", "PEBBLES_S", "PEBBLES_M",
        "PEBBLES_L",  "PEBBLES_XL",
    ]
    PEBBLE_FV          = 50_000  # structural fair value (sum of all 5 mids)
    PEBBLE_RESID_ENTRY = 10.0    # |residual| threshold to enter
    PEBBLE_MIN_EDGE    = 0.0     # minimum per-product edge to enter
    PEBBLE_HOLD_TICKS  = 10_000  # hold duration before exit
    PEBBLE_ENTRY_CLIP  = 10      # max qty per entry
    PEBBLE_FORCE_FLAT  = 995_000 # absolute timestamp at which all positions forced flat

    # ── 2. UV Visors ──────────────────────────────────────────────────────────
    VISOR_LEADER   = "UV_VISOR_RED"
    VISOR_FOLLOWER = "UV_VISOR_YELLOW"
    VISOR_LAG      = 250
    VISOR_ZWIN     = 500
    VISOR_ENTRY_Z  = 2.25
    VISOR_HOLD     = 1500

    # ── 3. Microchips ─────────────────────────────────────────────────────────
    CHIP_TRI  = "MICROCHIP_TRIANGLE"
    CHIP_SQ   = "MICROCHIP_SQUARE"
    CHIP_RECT = "MICROCHIP_RECTANGLE"
    CHIP_LEGS    = ["MICROCHIP_TRIANGLE", "MICROCHIP_SQUARE", "MICROCHIP_RECTANGLE"]
    CHIP_ZWIN    = 1000
    CHIP_ENTRY_Z = 2.5
    CHIP_EXIT_Z  = 0.0
    CHIP_HOLD    = 1000
    CHIP_LONG  = {"MICROCHIP_TRIANGLE": 10,  "MICROCHIP_SQUARE": -5, "MICROCHIP_RECTANGLE": -5}
    CHIP_SHORT = {"MICROCHIP_TRIANGLE": -10, "MICROCHIP_SQUARE":  5, "MICROCHIP_RECTANGLE":  5}

    # ── 4. Sleep Pods ─────────────────────────────────────────────────────────
    POD_SYNTH   = ["SLEEP_POD_POLYESTER", "SLEEP_POD_NYLON"]
    POD_NATURAL = ["SLEEP_POD_SUEDE", "SLEEP_POD_LAMB_WOOL", "SLEEP_POD_COTTON"]
    POD_ZWIN    = 2500
    POD_ENTRY_Z = 1.25
    POD_EXIT_Z  = 0.0
    POD_HOLD    = 2500
    POD_LONG  = {
        "SLEEP_POD_POLYESTER": 9,  "SLEEP_POD_NYLON": 9,
        "SLEEP_POD_SUEDE": -6,     "SLEEP_POD_LAMB_WOOL": -6, "SLEEP_POD_COTTON": -6,
    }
    POD_SHORT = {
        "SLEEP_POD_POLYESTER": -9, "SLEEP_POD_NYLON": -9,
        "SLEEP_POD_SUEDE": 6,      "SLEEP_POD_LAMB_WOOL": 6,  "SLEEP_POD_COTTON": 6,
    }

    # ── 5. Panels ─────────────────────────────────────────────────────────────
    PANEL_LEGS    = ["PANEL_2X4", "PANEL_4X4"]
    PANEL_2X4     = "PANEL_2X4"
    PANEL_4X4     = "PANEL_4X4"
    PANEL_ZWIN    = 1000
    PANEL_ENTRY_Z = 1.5
    PANEL_HOLD    = 1500
    PANEL_LONG  = {"PANEL_2X4": 10,  "PANEL_4X4": 10}
    PANEL_SHORT = {"PANEL_2X4": -10, "PANEL_4X4": -10}

    # ── 6. Snackpacks ─────────────────────────────────────────────────────────
    # Signal:  z-score( mid(CHOC) + mid(VAN) − mid(RASP) )  — unchanged
    # Trade:   VAN −10, RASP +10 on HIGH_Z_SELL.  CHOC position = 0.
    SNACK_CHOC = "SNACKPACK_CHOCOLATE"
    SNACK_VAN  = "SNACKPACK_VANILLA"
    SNACK_RASP = "SNACKPACK_RASPBERRY"
    SNACK_LEGS    = ["SNACKPACK_CHOCOLATE", "SNACKPACK_VANILLA", "SNACKPACK_RASPBERRY"]
    SNACK_ZWIN    = 1000    # widened from 500 — more stable reference mean
    SNACK_ENTRY_Z = 2.5
    SNACK_EXIT_Z  = 0.0
    SNACK_HOLD    = 500
    # CHOC: 0 — used only in signal, never traded
    SNACK_SHORT = {"SNACKPACK_CHOCOLATE": 0, "SNACKPACK_VANILLA": -10, "SNACKPACK_RASPBERRY": 10}

    # ── 7. Domestic Robots ────────────────────────────────────────────────────
    # Lead-lag momentum: leader's abnormal move predicts follower's direction.
    # Only target products are traded; lead products are information-only.
    # invert_signal=True because the relationship is same-direction (not MR):
    #   leader moves up strongly → buy follower (raw z positive → go long).
    # Parameters discovered empirically; lag and window are the free knobs per
    # pair — entry_z / exit_z / max_hold follow standard module conventions.
    ROBOT_CONFIGS = [
        {
            "name":          "robots_primary_ironing_to_dishes",
            "enabled":       True,
            "lead":          "ROBOT_IRONING",
            "target":        "ROBOT_DISHES",
            "lag":           100,    # ticks: how far back to measure lead move
            "window":        100,    # rolling window for z-score normalisation
            "entry_z":       2.75,
            "exit_z":        0.0,    # exit when signal crosses back through zero
            "qty":           10,
            "max_hold":      1000,
            "invert_signal": True,   # same-direction, not contrarian
        },
        {
            "name":          "robots_secondary_vacuuming_to_mopping",
            "enabled":       True,
            "lead":          "ROBOT_VACUUMING",
            "target":        "ROBOT_MOPPING",
            "lag":           20,     # faster leader: 20-tick lag sufficient
            "window":        100,
            "entry_z":       3.0,
            "exit_z":        0.0,
            "qty":           10,
            "max_hold":      750,
            "invert_signal": True,
        },
    ]

    LIMIT    = 10
    EOD_TICK = 990_000

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _best_ask(self, od: OrderDepth) -> Optional[int]:
        return min(od.sell_orders) if od.sell_orders else None

    def _best_bid(self, od: OrderDepth) -> Optional[int]:
        return max(od.buy_orders) if od.buy_orders else None

    def _mid(self, od: OrderDepth) -> Optional[float]:
        a = self._best_ask(od)
        b = self._best_bid(od)
        if a is not None and b is not None:
            return (a + b) / 2.0
        return None

    def _ask_qty(self, od: OrderDepth, price: int) -> int:
        """Available volume at ask price (positive)."""
        return -od.sell_orders.get(price, 0)

    def _bid_qty(self, od: OrderDepth, price: int) -> int:
        """Available volume at bid price (positive)."""
        return od.buy_orders.get(price, 0)

    def _z(self, series: list) -> Optional[float]:
        """Z-score of last element; sample std (ddof=1); min 30 samples."""
        if len(series) < 30:
            return None
        mu = statistics.mean(series)
        sd = statistics.stdev(series)
        if sd < 1e-9:
            return None
        return (series[-1] - mu) / sd

    def _go_to(self, symbol: str, target: int,
                state: TradingState, orders: dict) -> None:
        """Aggressive order: crosses ask (buy) or bid (sell)."""
        od = state.order_depths.get(symbol)
        if od is None:
            return
        delta = target - state.position.get(symbol, 0)
        if delta == 0:
            return
        price = self._best_ask(od) if delta > 0 else self._best_bid(od)
        if price is None:
            return
        orders.setdefault(symbol, []).append(Order(symbol, price, delta))

    # ─────────────────────────────────────────────────────────────────────────
    # State initialiser
    # ─────────────────────────────────────────────────────────────────────────

    def _init(self) -> dict:
        def sm() -> dict:
            return {"active": False, "side": 0, "hold": 0}
        return {
            "pebbles": {"entry_ts": {}},
            "visor":   {**sm(), "red_mids": [], "red_moves": []},
            "chips":   {**sm(), "sig": []},
            "pods":    {**sm(), "sig": []},
            "panels":  {**sm(), "sig": []},
            "snacks":  {**sm(), "sig": []},
            # One sub-dict per config entry, keyed by config index position
            "robots":  [
                {**sm(), "lead_mids": [], "lead_moves": []}
                for _ in self.ROBOT_CONFIGS
            ],
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Entry point
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, state: TradingState):

        # ── End-of-day flatten ────────────────────────────────────────────────
        if state.timestamp % 1_000_000 >= self.EOD_TICK:
            orders: dict = {}
            for symbol, pos in state.position.items():
                if pos != 0:
                    self._go_to(symbol, 0, state, orders)
            data = jsonpickle.decode(state.traderData) if state.traderData else self._init()
            for module in ["visor", "chips", "pods", "panels", "snacks"]:
                if module in data:
                    data[module].update(active=False, hold=0, side=0)
            if "robots" in data:
                for st in data["robots"]:
                    st.update(active=False, hold=0, side=0)
            return orders, 0, jsonpickle.encode(data)

        # ── Normal tick ───────────────────────────────────────────────────────
        data = jsonpickle.decode(state.traderData) if state.traderData else {}

        # Migrate: add any missing top-level keys from older batches
        for k, v in self._init().items():
            if k not in data:
                data[k] = v

        # Migrate: clean up batch-3 chips fields that no longer exist
        for stale_key in ("exiting", "exit_ticks"):
            data["chips"].pop(stale_key, None)

        # Migrate: replace batch-4 pebbles VW-history with entry_ts dict
        if "history" in data["pebbles"]:
            data["pebbles"] = {"entry_ts": {}}

        orders: dict = {}

        self._pebbles(state, data["pebbles"], orders)
        self._visor(state, data["visor"], orders)
        self._chips(state, data["chips"], orders)
        self._pods(state, data["pods"], orders)
        self._panels(state, data["panels"], orders)
        self._snacks(state, data["snacks"], orders)
        self._robots(state, data["robots"], orders)

        return orders, 0, jsonpickle.encode(data)

    # ─────────────────────────────────────────────────────────────────────────
    # Module 1 — Pebbles: VW-rolling-mean fair + symmetric basket trading
    # ─────────────────────────────────────────────────────────────────────────

    def _pebbles(self, state: TradingState, pb: dict, orders: dict) -> None:
        od = state.order_depths
        entry_ts: dict = pb["entry_ts"]

        # ── Snapshot ──────────────────────────────────────────────────────────
        snap = {}
        for p in self.PEBBLES:
            ob = od.get(p)
            if ob is None:
                return
            bid = self._best_bid(ob)
            ask = self._best_ask(ob)
            mid = self._mid(ob)
            if bid is None or ask is None or mid is None:
                return
            snap[p] = {
                "bid":     bid,
                "ask":     ask,
                "mid":     mid,
                "bid_vol": self._bid_qty(ob, bid),
                "ask_vol": self._ask_qty(ob, ask),
                "pos":     state.position.get(p, 0),
            }

        # residual = how far sum-of-mids is from the structural FV
        total_mid   = sum(snap[p]["mid"] for p in self.PEBBLES)
        residual_mid = self.PEBBLE_FV - total_mid

        for p in self.PEBBLES:
            fair = snap[p]["mid"] + residual_mid
            snap[p]["fair"]      = fair
            snap[p]["buy_edge"]  = fair - snap[p]["ask"]
            snap[p]["sell_edge"] = snap[p]["bid"] - fair

        ordered_by_mid     = sorted(self.PEBBLES, key=lambda p: snap[p]["mid"])
        lowest_mid_product = ordered_by_mid[0]
        highest_mid_product = ordered_by_mid[-1]

        # ── Exit logic ────────────────────────────────────────────────────────
        for p in self.PEBBLES:
            pos = snap[p]["pos"]
            if pos == 0:
                entry_ts.pop(p, None)
                continue

            ts_open    = entry_ts.get(p, state.timestamp)
            hold_done  = state.timestamp - ts_open >= self.PEBBLE_HOLD_TICKS
            force_flat = state.timestamp >= self.PEBBLE_FORCE_FLAT

            if pos > 0 and (hold_done or force_flat):
                qty = min(pos, snap[p]["bid_vol"])
                if qty > 0:
                    orders.setdefault(p, []).append(Order(p, snap[p]["bid"], -qty))
                    if qty == pos:
                        entry_ts.pop(p, None)
            elif pos < 0 and (hold_done or force_flat):
                qty = min(-pos, snap[p]["ask_vol"])
                if qty > 0:
                    orders.setdefault(p, []).append(Order(p, snap[p]["ask"], qty))
                    if qty == -pos:
                        entry_ts.pop(p, None)

        # ── Entry: only through the current extremes of the curve ─────────────
        if state.timestamp < self.PEBBLE_FORCE_FLAT:
            # Long the highest-mid leg when basket is cheap
            long_pos = snap[highest_mid_product]["pos"]
            if (
                residual_mid >= self.PEBBLE_RESID_ENTRY
                and long_pos == 0
                and snap[highest_mid_product]["buy_edge"] >= self.PEBBLE_MIN_EDGE
            ):
                qty = min(
                    self.PEBBLE_ENTRY_CLIP,
                    self.LIMIT,
                    snap[highest_mid_product]["ask_vol"],
                )
                if qty > 0:
                    orders.setdefault(highest_mid_product, []).append(
                        Order(highest_mid_product, snap[highest_mid_product]["ask"], qty)
                    )
                    entry_ts[highest_mid_product] = state.timestamp

            # Short the lowest-mid leg when basket is rich
            short_pos = snap[lowest_mid_product]["pos"]
            if (
                residual_mid <= -self.PEBBLE_RESID_ENTRY
                and short_pos == 0
                and snap[lowest_mid_product]["sell_edge"] >= self.PEBBLE_MIN_EDGE
            ):
                qty = min(
                    self.PEBBLE_ENTRY_CLIP,
                    self.LIMIT,
                    snap[lowest_mid_product]["bid_vol"],
                )
                if qty > 0:
                    orders.setdefault(lowest_mid_product, []).append(
                        Order(lowest_mid_product, snap[lowest_mid_product]["bid"], -qty)
                    )
                    entry_ts[lowest_mid_product] = state.timestamp

    # ─────────────────────────────────────────────────────────────────────────
    # Module 2 — UV Visors: RED → YELLOW inverse lead-lag (unchanged)
    # ─────────────────────────────────────────────────────────────────────────

    def _visor(self, state: TradingState, v: dict, orders: dict) -> None:
        od = state.order_depths

        if v["active"] and state.position.get(self.VISOR_FOLLOWER, 0) == 0:
            v.update(active=False, side=0, hold=0)

        if self.VISOR_LEADER in od:
            m = self._mid(od[self.VISOR_LEADER])
            if m is not None:
                v["red_mids"].append(m)
                cap = self.VISOR_LAG + self.VISOR_ZWIN
                if len(v["red_mids"]) > cap:
                    v["red_mids"] = v["red_mids"][-cap:]

        if len(v["red_mids"]) > self.VISOR_LAG:
            move = v["red_mids"][-1] - v["red_mids"][-(self.VISOR_LAG + 1)]
            v["red_moves"].append(move)
            if len(v["red_moves"]) > self.VISOR_ZWIN:
                v["red_moves"] = v["red_moves"][-self.VISOR_ZWIN:]

        if self.VISOR_FOLLOWER not in od:
            return

        z = self._z(v["red_moves"])
        if z is None:
            return

        if v["active"]:
            v["hold"] += 1
            if v["hold"] >= self.VISOR_HOLD:
                self._go_to(self.VISOR_FOLLOWER, 0, state, orders)
                v.update(active=False, side=0, hold=0)
        else:
            if z >= self.VISOR_ENTRY_Z:
                self._go_to(self.VISOR_FOLLOWER, -self.LIMIT, state, orders)
                v.update(active=True, side=-1, hold=0)
            elif z <= -self.VISOR_ENTRY_Z:
                self._go_to(self.VISOR_FOLLOWER, self.LIMIT, state, orders)
                v.update(active=True, side=1, hold=0)

    # ─────────────────────────────────────────────────────────────────────────
    # Module 3 — Microchips: aggressive exits restored (batch 2 behaviour)
    # ─────────────────────────────────────────────────────────────────────────

    def _chips(self, state: TradingState, c: dict, orders: dict) -> None:
        od = state.order_depths

        # Position reconciliation
        if c["active"] and all(state.position.get(p, 0) == 0 for p in self.CHIP_LEGS):
            c.update(active=False, side=0, hold=0)

        for p in self.CHIP_LEGS:
            if p not in od:
                return

        tri  = self._mid(od[self.CHIP_TRI])
        sq   = self._mid(od[self.CHIP_SQ])
        rect = self._mid(od[self.CHIP_RECT])
        if None in (tri, sq, rect) or min(tri, sq, rect) <= 0:
            return

        sig = math.log(tri / ((sq + rect) / 2.0))
        c["sig"].append(sig)
        if len(c["sig"]) > self.CHIP_ZWIN:
            c["sig"] = c["sig"][-self.CHIP_ZWIN:]

        z = self._z(c["sig"])
        if z is None:
            return

        if c["active"]:
            c["hold"] += 1
            exit_trade = c["hold"] >= self.CHIP_HOLD
            if not exit_trade:
                if c["side"] == -1 and z <= self.CHIP_EXIT_Z:
                    exit_trade = True
                elif c["side"] == 1 and z >= self.CHIP_EXIT_Z:
                    exit_trade = True
            if exit_trade:
                # Aggressive exit: cross the spread immediately
                for p in self.CHIP_LEGS:
                    self._go_to(p, 0, state, orders)
                c.update(active=False, side=0, hold=0)
        else:
            if z >= self.CHIP_ENTRY_Z:
                for p, tgt in self.CHIP_SHORT.items():
                    self._go_to(p, tgt, state, orders)
                c.update(active=True, side=-1, hold=0)
            elif z <= -self.CHIP_ENTRY_Z:
                for p, tgt in self.CHIP_LONG.items():
                    self._go_to(p, tgt, state, orders)
                c.update(active=True, side=1, hold=0)

    # ─────────────────────────────────────────────────────────────────────────
    # Module 4 — Sleep Pods: unchanged
    # ─────────────────────────────────────────────────────────────────────────

    def _pods(self, state: TradingState, d: dict, orders: dict) -> None:
        od = state.order_depths
        all_prods = self.POD_SYNTH + self.POD_NATURAL

        if d["active"] and all(state.position.get(p, 0) == 0 for p in all_prods):
            d.update(active=False, side=0, hold=0)

        for prod in all_prods:
            if prod not in od:
                return

        mids = {}
        for prod in all_prods:
            m = self._mid(od[prod])
            if m is None:
                return
            mids[prod] = m

        synth_avg   = sum(mids[p] for p in self.POD_SYNTH)   / len(self.POD_SYNTH)
        natural_avg = sum(mids[p] for p in self.POD_NATURAL) / len(self.POD_NATURAL)
        sig = synth_avg - natural_avg

        d["sig"].append(sig)
        if len(d["sig"]) > self.POD_ZWIN:
            d["sig"] = d["sig"][-self.POD_ZWIN:]

        z = self._z(d["sig"])
        if z is None:
            return

        if d["active"]:
            d["hold"] += 1
            exit_trade = d["hold"] >= self.POD_HOLD
            if not exit_trade:
                if d["side"] == 1  and z >= self.POD_EXIT_Z:  exit_trade = True
                elif d["side"] == -1 and z <= self.POD_EXIT_Z: exit_trade = True
            if exit_trade:
                for prod in all_prods:
                    self._go_to(prod, 0, state, orders)
                d.update(active=False, side=0, hold=0)
        else:
            if z <= -self.POD_ENTRY_Z:
                for prod, tgt in self.POD_LONG.items():
                    self._go_to(prod, tgt, state, orders)
                d.update(active=True, side=1, hold=0)
            elif z >= self.POD_ENTRY_Z:
                for prod, tgt in self.POD_SHORT.items():
                    self._go_to(prod, tgt, state, orders)
                d.update(active=True, side=-1, hold=0)

    # ─────────────────────────────────────────────────────────────────────────
    # Module 5 — Panels: unchanged
    # ─────────────────────────────────────────────────────────────────────────

    def _panels(self, state: TradingState, pn: dict, orders: dict) -> None:
        od = state.order_depths

        if pn["active"] and all(state.position.get(p, 0) == 0 for p in self.PANEL_LEGS):
            pn.update(active=False, side=0, hold=0)

        for prod in self.PANEL_LEGS:
            if prod not in od:
                return

        m2x4 = self._mid(od[self.PANEL_2X4])
        m4x4 = self._mid(od[self.PANEL_4X4])
        if m2x4 is None or m4x4 is None:
            return

        sig = m2x4 + m4x4
        pn["sig"].append(sig)
        if len(pn["sig"]) > self.PANEL_ZWIN:
            pn["sig"] = pn["sig"][-self.PANEL_ZWIN:]

        z = self._z(pn["sig"])
        if z is None:
            return

        if pn["active"]:
            pn["hold"] += 1
            if pn["hold"] >= self.PANEL_HOLD:
                for prod in self.PANEL_LEGS:
                    self._go_to(prod, 0, state, orders)
                pn.update(active=False, side=0, hold=0)
        else:
            if z >= self.PANEL_ENTRY_Z:
                for prod, tgt in self.PANEL_SHORT.items():
                    self._go_to(prod, tgt, state, orders)
                pn.update(active=True, side=-1, hold=0)
            elif z <= -self.PANEL_ENTRY_Z:
                for prod, tgt in self.PANEL_LONG.items():
                    self._go_to(prod, tgt, state, orders)
                pn.update(active=True, side=1, hold=0)

    # ─────────────────────────────────────────────────────────────────────────
    # Module 6 — Snackpacks: wider window, CHOC position removed
    # ─────────────────────────────────────────────────────────────────────────

    def _snacks(self, state: TradingState, s: dict, orders: dict) -> None:
        od = state.order_depths

        # Position reconciliation
        if s["active"] and all(state.position.get(p, 0) == 0 for p in self.SNACK_LEGS):
            s.update(active=False, side=0, hold=0)

        for prod in self.SNACK_LEGS:
            if prod not in od:
                return

        mc = self._mid(od[self.SNACK_CHOC])
        mv = self._mid(od[self.SNACK_VAN])
        mr = self._mid(od[self.SNACK_RASP])
        if None in (mc, mv, mr):
            return

        # Signal includes CHOC for informational value — CHOC price does
        # contain information about whether the basket is dislocated.
        # CHOC is just not traded (position = 0) because it doesn't
        # reliably mean-revert on the short side.
        sig = mc + mv - mr
        s["sig"].append(sig)
        if len(s["sig"]) > self.SNACK_ZWIN:
            s["sig"] = s["sig"][-self.SNACK_ZWIN:]

        z = self._z(s["sig"])
        if z is None:
            return

        if s["active"]:
            s["hold"] += 1
            exit_trade = s["hold"] >= self.SNACK_HOLD
            if not exit_trade:
                if s["side"] == -1 and z <= self.SNACK_EXIT_Z:
                    exit_trade = True
                elif s["side"] == 1 and z >= self.SNACK_EXIT_Z:
                    exit_trade = True
            if exit_trade:
                # Exit all legs — CHOC delta will be 0 (never entered), clean exit
                for prod in self.SNACK_LEGS:
                    self._go_to(prod, 0, state, orders)
                s.update(active=False, side=0, hold=0)
        else:
            # HIGH_Z_SELL only: spread is rich, short VAN, long RASP, leave CHOC flat
            if z >= self.SNACK_ENTRY_Z:
                for prod, tgt in self.SNACK_SHORT.items():
                    self._go_to(prod, tgt, state, orders)
                s.update(active=True, side=-1, hold=0)

    # ─────────────────────────────────────────────────────────────────────────
    # Module 7 — Domestic Robots: cross-product lead-lag momentum
    #
    # Signal construction (per alpha):
    #   1. Record lead product mid each tick.
    #   2. lead_move = mid_now − mid_{now − lag}  (raw price momentum of leader)
    #   3. z = zscore(lead_moves[-window:])       (normalise vs recent behaviour)
    #   4. effective_signal = −z  (same-direction: leader up → follower lags up)
    #
    # Entry  when |effective_signal| ≥ entry_z  and  target is flat.
    # Exit   when |effective_signal| ≤ exit_z   OR   hold ≥ max_hold.
    # Only target products receive orders; lead products are never traded.
    # ─────────────────────────────────────────────────────────────────────────

    def _robots(self, state: TradingState, robot_states: list, orders: dict) -> None:
        od = state.order_depths

        for cfg, st in zip(self.ROBOT_CONFIGS, robot_states):
            if not cfg["enabled"]:
                continue

            lead   = cfg["lead"]
            target = cfg["target"]

            if lead not in od or target not in od:
                continue

            lead_mid = self._mid(od[lead])
            if lead_mid is None:
                continue

            # ── Accumulate lead price history ─────────────────────────────────
            st["lead_mids"].append(lead_mid)
            cap = cfg["lag"] + cfg["window"] + 5   # minimal buffer, no extra bloat
            if len(st["lead_mids"]) > cap:
                st["lead_mids"] = st["lead_mids"][-cap:]

            if len(st["lead_mids"]) <= cfg["lag"]:
                continue   # not enough history yet to compute a move

            # ── Compute normalised momentum signal ────────────────────────────
            lead_move = st["lead_mids"][-1] - st["lead_mids"][-(cfg["lag"] + 1)]
            st["lead_moves"].append(lead_move)
            if len(st["lead_moves"]) > cfg["window"]:
                st["lead_moves"] = st["lead_moves"][-cfg["window"]:]

            raw_z = self._z(st["lead_moves"])
            if raw_z is None:
                continue

            # Invert so that a strong upward leader move signals BUY follower
            sig = -raw_z if cfg["invert_signal"] else raw_z

            # ── Position reconciliation ───────────────────────────────────────
            if st["active"] and state.position.get(target, 0) == 0:
                st.update(active=False, side=0, hold=0)

            # ── Exit ──────────────────────────────────────────────────────────
            if st["active"]:
                st["hold"] += 1
                exit_trade = st["hold"] >= cfg["max_hold"]
                if not exit_trade and abs(sig) <= cfg["exit_z"]:
                    exit_trade = True
                if exit_trade:
                    self._go_to(target, 0, state, orders)
                    st.update(active=False, side=0, hold=0)

            # ── Entry (only when flat) ────────────────────────────────────────
            else:
                if sig >= cfg["entry_z"]:
                    self._go_to(target, cfg["qty"], state, orders)
                    st.update(active=True, side=1, hold=0)
                elif sig <= -cfg["entry_z"]:
                    self._go_to(target, -cfg["qty"], state, orders)
                    st.update(active=True, side=-1, hold=0)