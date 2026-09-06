"""Коннектор Binance Futures Testnet (USDT-M бессрочные фьючерсы, демо-деньги).

Зачем: у Alpaca крипта — только лонг за наличные. Здесь есть шорты и плечо
до 125x. Плечо задаётся на позицию; notional — размер позиции в USDT,
залог (маржа) = notional / плечо. При плече 50x движение на 2% против
позиции = ликвидация. Только testnet: sandbox-режим включён жёстко.
"""
from __future__ import annotations

from marketpulse.config import settings

# внутренний тикер -> символ бессрочного контракта в ccxt
_SYMBOLS = {"BTC-USD": "BTC/USDT:USDT", "ETH-USD": "ETH/USDT:USDT", "SOL-USD": "SOL/USDT:USDT"}


def binance_symbol(symbol: str) -> str | None:
    return _SYMBOLS.get(symbol)


def client():
    """ccxt-клиент в sandbox-режиме или None, если ключей нет."""
    if not (settings.binance_testnet_api_key and settings.binance_testnet_api_secret):
        return None
    try:
        import ccxt

        ex = ccxt.binanceusdm({
            "apiKey": settings.binance_testnet_api_key,
            "secret": settings.binance_testnet_api_secret,
            "options": {"defaultType": "future"},
            "enableRateLimit": True,
        })
        ex.set_sandbox_mode(True)  # жёстко: только тестовая сеть
        return ex
    except Exception:  # noqa: BLE001
        return None


def open_position(ex, symbol: str, direction: str, notional: float, leverage: int) -> tuple[str | None, str | None]:
    """Рыночный ордер: (id, ошибка). notional — размер позиции в USDT."""
    sym = binance_symbol(symbol)
    if sym is None:
        return None, f"{symbol}: нет контракта на Binance"
    try:
        ex.set_leverage(int(leverage), sym)
        price = float(ex.fetch_ticker(sym)["last"])
        amount = float(ex.amount_to_precision(sym, notional / price))
        if amount <= 0:
            return None, "размер меньше минимального лота"
        side = "buy" if direction == "long" else "sell"
        order = ex.create_order(sym, "market", side, amount)
        return f"binance:{order['id']}", None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:160]}"


def close_position(ex, symbol: str, direction: str, notional: float) -> str | None:
    """Закрытие обратным reduce-only ордером."""
    sym = binance_symbol(symbol)
    if sym is None:
        return None
    try:
        price = float(ex.fetch_ticker(sym)["last"])
        amount = float(ex.amount_to_precision(sym, notional / price))
        side = "sell" if direction == "long" else "buy"
        order = ex.create_order(sym, "market", side, amount, params={"reduceOnly": True})
        return f"binance:{order['id']}"
    except Exception:  # noqa: BLE001
        return None


def positions(ex) -> list[tuple[str, float]]:
    """[(внутренний тикер, размер в USDT со знаком)] по открытым контрактам."""
    out = []
    try:
        inv = {v: k for k, v in _SYMBOLS.items()}
        for p in ex.fetch_positions(list(_SYMBOLS.values())):
            amt = float(p.get("contracts") or 0)
            if amt == 0:
                continue
            sym = inv.get(p["symbol"])
            if sym:
                signed = float(p.get("notional") or 0) * (1 if p.get("side") == "long" else -1)
                out.append((sym, signed))
    except Exception:  # noqa: BLE001
        pass
    return out


def close_all(ex, symbol: str) -> bool:
    sym = binance_symbol(symbol)
    if sym is None:
        return False
    try:
        for p in ex.fetch_positions([sym]):
            amt = float(p.get("contracts") or 0)
            if amt == 0:
                continue
            side = "sell" if p.get("side") == "long" else "buy"
            ex.create_order(sym, "market", side, amt, params={"reduceOnly": True})
        return True
    except Exception:  # noqa: BLE001
        return False
