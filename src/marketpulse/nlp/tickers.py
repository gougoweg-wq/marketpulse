"""Извлечение тикеров из текста.

Два пути: явное упоминание тикера ($AAPL, NASDAQ:AAPL) и название
компании ("Apple", "Nvidia"). Короткие тикеры, совпадающие с обычными
словами (V, MA, KO...), принимаем только в явной форме с $ — иначе
каждая новость про "ma" и "v" привязалась бы к Mastercard и Visa.
"""
from __future__ import annotations

import re

from marketpulse.config import settings
from marketpulse.ingest.feeds import TICKER_QUERY

# тикеры, которые опасно ловить как голые слова
AMBIGUOUS = {"V", "MA", "KO", "BA", "DIS", "GS", "USO", "ALL", "ON", "IT", "A"}

_DOLLAR_RE = re.compile(r"[$]([A-Z]{1,5})\b")
_EXCH_RE = re.compile(r"\b(?:NYSE|NASDAQ|AMEX)[:\s]([A-Z]{1,5})\b")

# название компании -> тикер, с вариантами
_NAME_PATTERNS: list[tuple[re.Pattern, str]] = []
for sym, name in TICKER_QUERY.items():
    base = name.split()[0]
    if len(base) < 4:            # "AMD", "S&P" — ловим только как тикер
        continue
    _NAME_PATTERNS.append((re.compile(rf"\b{re.escape(base)}\b", re.I), sym))


def extract_tickers(text: str) -> list[str]:
    watch = set(settings.watchlist)
    found: set[str] = set()

    for m in _DOLLAR_RE.finditer(text):
        if m.group(1) in watch:
            found.add(m.group(1))
    for m in _EXCH_RE.finditer(text):
        if m.group(1) in watch:
            found.add(m.group(1))

    # голое упоминание тикера словом — только для «безопасных»
    for sym in watch - AMBIGUOUS - found:
        if re.search(rf"\b{sym}\b", text):
            found.add(sym)

    for pattern, sym in _NAME_PATTERNS:
        if sym in watch and pattern.search(text):
            found.add(sym)

    return sorted(found)
