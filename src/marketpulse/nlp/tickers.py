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

# названия-омонимы: "visa rules", "Boris Johnson", "gold medal", "Nasdaq closes"
# ничего не говорят о компании — для них нужен контекст
_CONTEXT_PATTERNS: dict[str, str] = {
    "V": r"\bVisa(?:'s| Inc| stock| shares| card network| earnings)\b",
    "MA": r"\bMastercard\b",
    "META": r"\bMeta(?:'s| Platforms| stock| shares| earnings| AI)\b",
    "INTC": r"\bIntel(?:'s| Corp| stock| shares| chips?| foundry| earnings)\b",
    "JNJ": r"Johnson\s*&\s*Johnson|\bJ&J\b",
    "IWM": r"Russell\s*2000",
    "QQQ": r"Nasdaq[- ]?100|Nasdaq Composite|\bQQQ\b",
    "GLD": r"\bgold (?:price|prices|futures|rally|rallies|miners|bullion|ETF)",
    "SLV": r"\bsilver (?:price|prices|futures|rally|ETF)",
    "TLT": r"Treasury (?:yields?|bonds?|market)|\bTreasuries\b",
    "USO": r"\b(?:crude|oil) (?:price|prices|futures)|\bWTI\b|\bBrent\b",
    "KO": r"Coca[- ]Cola",
    "DIS": r"\bDisney\b",
    "BA": r"\bBoeing\b",
    "GS": r"Goldman Sachs",
    "GDX": r"\bgold miners?\b|\bgold mining\b|\bNewmont\b|\bBarrick\b",
    "PPLT": r"\bplatinum (?:price|prices|futures|market)",
    "CPER": r"\bcopper (?:price|prices|futures|market|demand)",
    "UNG": r"\bnatural gas (?:price|prices|futures|market)|\bnat[- ]gas\b|\bLNG\b",
    "URA": r"\buranium\b|\bnuclear (?:power|energy|fuel)\b",
    "DBA": r"\b(?:wheat|corn|soybean|cattle|sugar|coffee) (?:price|prices|futures)",
}
_NAME_PATTERNS: list[tuple[re.Pattern, str]] = []
for sym, name in TICKER_QUERY.items():
    if sym in _CONTEXT_PATTERNS:
        _NAME_PATTERNS.append((re.compile(_CONTEXT_PATTERNS[sym], re.I), sym))
        continue
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
