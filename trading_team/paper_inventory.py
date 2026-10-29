"""FIFO acquisition quantities derived from trades; not execution certification."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import math

INVENTORY_POLICY_VERSION = "fifo_acquisition.v1"


def _day(value):
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except (TypeError, ValueError):
        pass
    raise ValueError("inventory date is invalid")


def _qty(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 or value != int(value)):
        raise ValueError("inventory quantity must be a positive integer")
    return int(value)


def available_quantity(position: dict, day: str) -> int:
    day = _day(day)
    total = _qty(position.get("qty"))
    if "lots" not in position:
        return total if _day(position.get("buy_day")) < day else 0
    lots = position["lots"]
    if not isinstance(lots, list) or not lots:
        raise ValueError("inventory lots missing")
    previous_id, previous_day, quantity, available = 0, "", 0, 0
    for lot in lots:
        identity, acquired, qty = _qty(lot.get("trade_id")), _day(lot.get("buy_day")), _qty(lot.get("qty"))
        if identity <= previous_id or acquired < previous_day or acquired > day:
            raise ValueError("inventory lots are duplicated, out of order or in the future")
        previous_id, previous_day = identity, acquired
        quantity += qty
        if acquired < day:
            available += qty
    if quantity != total or previous_day != position.get("buy_day"):
        raise ValueError("inventory lot quantities or acquisition date disagree with position")
    return available


def consume_position(position: dict, qty: int, day: str) -> None:
    qty = _qty(qty)
    if qty > available_quantity(position, day):
        raise ValueError("inventory T+1 available quantity exceeded")
    if "lots" in position:
        remaining, lots = qty, []
        for lot in position["lots"]:
            used = min(remaining, lot["qty"]) if lot["buy_day"] < day else 0
            remaining -= used
            if lot["qty"] > used:
                lots.append({**lot, "qty": lot["qty"] - used})
        position["lots"] = lots
        position["buy_day"] = lots[-1]["buy_day"] if lots else None
    position["qty"] -= qty


def read_trades(conn) -> list[dict]:
    fields = ("id", "day", "code", "side", "qty")
    rows = conn.execute("SELECT id,day,code,side,qty FROM trades ORDER BY id")
    return [dict(zip(fields, row)) for row in rows]


def inventory_basis(trades: list[dict], day: str) -> dict:
    day = _day(day)
    positions, source = {}, []
    previous_id, previous_day = 0, ""
    for trade in trades:
        identity, acquired, qty = _qty(trade["id"]), _day(trade["day"]), _qty(trade["qty"])
        code, side = trade["code"], trade["side"]
        if (not isinstance(code, str) or not code or side not in ("buy", "sell")
                or identity <= previous_id or acquired < previous_day or acquired > day):
            raise ValueError("inventory trade sequence, symbol, side or date is invalid")
        previous_id, previous_day = identity, acquired
        source.append({"id": identity, "day": acquired, "code": code, "side": side, "qty": qty})
        current = positions.get(code, {"qty": 0, "buy_day": None, "lots": []})
        if side == "buy":
            current["qty"] += qty
            current["buy_day"] = acquired
            current["lots"].append({"trade_id": identity, "buy_day": acquired, "qty": qty})
            positions[code] = current
        else:
            consume_position(current, qty, acquired)
            if not current["qty"]:
                positions.pop(code, None)
    trade_hash = hashlib.sha256(json.dumps(source, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {"policy": INVENTORY_POLICY_VERSION, "as_of": day, "trade_hash": trade_hash,
            "trade_count": len(source), "positions": positions}


def attach_inventory(positions: dict, basis: dict) -> None:
    expected = basis["positions"]
    if (positions.keys() != expected.keys()
            or any(_qty(row["qty"]) != expected[code]["qty"] or row.get("buy_day") != expected[code]["buy_day"]
                   for code, row in positions.items())):
        raise ValueError("inventory position projection does not match trade ledger")
    for code, row in positions.items():
        row["qty"] = expected[code]["qty"]
        row["lots"] = deepcopy(expected[code]["lots"])


def verify_receipt_inventory(conn, day: str, receipt: dict) -> None:
    ledger = read_trades(conn)
    opening = inventory_basis([row for row in ledger if row["day"] < day], day)
    closing = inventory_basis([row for row in ledger if row["day"] <= day], day)
    if receipt.get("inventory_basis") != opening or receipt.get("inventory_closing") != closing:
        raise ValueError("Paper inventory evidence changed; reconcile before retry")
    last_day = conn.execute("SELECT MAX(day) FROM account WHERE day!='init'").fetchone()[0] or day
    current = {row[0]: {"qty": row[1], "buy_day": row[2]}
               for row in conn.execute("SELECT code,qty,buy_day FROM positions")}
    attach_inventory(current, inventory_basis(ledger, last_day))
