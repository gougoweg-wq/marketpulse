"""Классы активов: у акций есть часы торгов, у крипты — нет.

Внутренний тикер крипты — в форме yfinance ("BTC-USD"); у брокера Alpaca
он же пишется "BTC/USD" (ордера) и "BTCUSD" (позиции).
"""
from __future__ import annotations

from marketpulse.config import settings


def is_crypto(symbol: str) -> bool:
    return symbol.endswith("-USD")


def all_symbols() -> list[str]:
    return list(settings.watchlist) + list(settings.crypto_watchlist)


def equity_symbols() -> list[str]:
    return list(settings.watchlist)


def alpaca_symbol(symbol: str) -> str:
    """Тикер в форме ордера Alpaca."""
    return symbol.replace("-USD", "/USD") if is_crypto(symbol) else symbol


def normalize_symbol(symbol: str) -> str:
    """Общий вид для сравнения с позициями брокера (BTCUSD == BTC/USD == BTC-USD)."""
    return symbol.replace("/", "").replace("-", "").upper()
