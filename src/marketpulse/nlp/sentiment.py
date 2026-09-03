"""Тональность финансового текста.

Стартуем с лексикона в духе Loughran-McDonald: обычные сентимент-словари
врут на финансовых текстах ("liability", "gross" — не негатив в финотчёте).
Словарь компактный, но заточен под заголовки рынка. Позже сюда можно
подключить трансформер (FinBERT) без изменения интерфейса.
"""
from __future__ import annotations

import math
import re

POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
    "jump", "jumps", "gain", "gains", "record", "upgrade", "upgraded",
    "outperform", "bullish", "growth", "profit", "profits", "strong",
    "exceed", "exceeds", "boom", "breakthrough", "buyback", "dividend",
    "raise", "raises", "raised", "top", "tops", "win", "wins", "approval",
    "approved", "expand", "expands", "рост", "взлет", "прибыль", "рекорд",
    "покупать", "ракета", "выше",
}
NEGATIVE = {
    "miss", "misses", "missed", "plunge", "plunges", "crash", "crashes",
    "fall", "falls", "drop", "drops", "sink", "sinks", "slump", "slumps",
    "downgrade", "downgraded", "underperform", "bearish", "loss", "losses",
    "weak", "lawsuit", "probe", "investigation", "fraud", "recall",
    "layoff", "layoffs", "cut", "cuts", "bankruptcy", "default", "warning",
    "warns", "fine", "fined", "halt", "halted", "tumble", "tumbles",
    "fear", "fears", "recession", "inflation", "падение", "обвал", "убыток",
    "продавать", "крах", "ниже", "риск",
}
INTENSIFIERS = {"very", "sharply", "massively", "hugely", "significantly", "резко", "сильно"}
NEGATORS = {"not", "no", "never", "не", "нет"}

_WORD_RE = re.compile(r"[a-zа-яё']+", re.I)


def score_sentiment(text: str) -> float:
    """Тональность в [-1, 1]. 0 — нейтрально."""
    words = _WORD_RE.findall(text.lower())
    if not words:
        return 0.0

    score = 0.0
    for i, w in enumerate(words):
        val = 0.0
        if w in POSITIVE:
            val = 1.0
        elif w in NEGATIVE:
            val = -1.0
        if val == 0.0:
            continue
        window = words[max(0, i - 2):i]
        if any(x in NEGATORS for x in window):
            val = -val * 0.8
        if any(x in INTENSIFIERS for x in window):
            val *= 1.5
        score += val

    # насыщение: одна-две сильные лексемы не должны давать ±1
    return math.tanh(score / 3.0)
