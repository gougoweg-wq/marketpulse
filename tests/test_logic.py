"""Тесты ключевой логики: дедупликация, тикеры, тональность, честность исходов."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"

import pytest  # noqa: E402

from marketpulse.db.session import init_db, db_session  # noqa: E402
from marketpulse.db.models import (  # noqa: E402
    Decision, DecisionReason, Direction, PriceBar,
)
from marketpulse.nlp.dedup import simhash64, hamming  # noqa: E402
from marketpulse.nlp.tickers import extract_tickers  # noqa: E402
from marketpulse.nlp.sentiment import score_sentiment  # noqa: E402
from marketpulse.model.engine import record_outcomes, ROUND_TRIP_COST  # noqa: E402
from marketpulse.model.learner import OnlineModel  # noqa: E402
from marketpulse.model.features import FEATURE_ORDER, clean_text  # noqa: E402

init_db()

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- NLP

def test_simhash_close_for_reprints():
    a = simhash64("Apple beats earnings expectations as iPhone sales surge in Q3")
    b = simhash64("Apple beats earnings expectations, iPhone sales surge in Q3 2026")
    c = simhash64("Oil prices crash after OPEC announces production increase")
    assert hamming(a, b) <= 8       # перепечатка склеивается
    assert hamming(a, c) > 8        # другая новость — нет


def test_tickers_dollar_and_name():
    assert "AAPL" in extract_tickers("Analysts upgrade $AAPL to buy")
    assert "AAPL" in extract_tickers("Apple announced a new product")
    # 'ma' как слово не должно цеплять Mastercard
    assert "MA" not in extract_tickers("ma il problema resta")
    assert "MA" in extract_tickers("$MA hit a record high")


def test_sentiment_direction():
    assert score_sentiment("Stocks surge to record high after strong earnings beat") > 0.2
    assert score_sentiment("Shares plunge as company warns of losses and layoffs") < -0.2
    assert abs(score_sentiment("The company held a meeting on Tuesday")) < 0.1


def test_clean_text_strips_foreign_agent_junk():
    junk = ("Рынок растёт. НАСТОЯЩИЙ МАТЕРИАЛ (ИНФОРМАЦИЯ) ПРОИЗВЕДЕН И РАСПРОСТРАНЕН "
            "ИНОСТРАННЫМ АГЕНТОМ ЛИБО КАСАЕТСЯ ДЕЯТЕЛЬНОСТИ ИНОСТРАННОГО АГЕНТА ИВАНОВА.")
    assert "АГЕНТ" not in clean_text(junk)
    assert "Рынок растёт" in clean_text(junk)


# ---------------------------------------------------------------- исходы

def _seed_bars(s, symbol: str, start: datetime, closes: list[float]):
    for i, c in enumerate(closes):
        s.add(PriceBar(symbol=symbol, interval="1h",
                       ts=(start + timedelta(hours=i)).replace(tzinfo=None),
                       open=c, high=c, low=c, close=c, volume=1000))


@pytest.fixture()
def no_model_save(monkeypatch):
    monkeypatch.setattr(OnlineModel, "save", lambda self: None)


def test_outcome_uses_first_bar_after_decision(no_model_save):
    """Честный вход: первый бар ПОСЛЕ решения, а не цена до новости."""
    with db_session() as s:
        # цена до решения 100 (модель её знать может), после решения 110 -> 120
        _seed_bars(s, "TST1", T0 - timedelta(hours=3), [98, 99, 100])
        _seed_bars(s, "TST1", T0 + timedelta(hours=1), [110, 112, 115, 118, 120])
        s.add(Decision(
            symbol="TST1", direction=Direction.long, reason=DecisionReason.model,
            confidence=0.7, features={k: 0.0 for k in FEATURE_ORDER},
            horizon_hours=4, created_at=T0.replace(tzinfo=None),
            entry_price=100.0,  # цена на момент решения (для размера позиции)
        ))
    record_outcomes(model=OnlineModel())
    with db_session() as s:
        d = s.query(Decision).filter_by(symbol="TST1").one()
        assert d.entry_price == 110.0          # перезаписан честным входом
        assert d.exit_price == 118.0           # бар на границе горизонта (T0+4h -> первый бар >= 16:00)
        expected = 118.0 / 110.0 - 1 - ROUND_TRIP_COST
        assert abs(d.realized_return - expected) < 1e-9


def test_short_direction_inverts_return(no_model_save):
    with db_session() as s:
        _seed_bars(s, "TST2", T0 - timedelta(hours=2), [50, 50])
        _seed_bars(s, "TST2", T0 + timedelta(hours=1), [50, 49, 48, 46, 45])
        s.add(Decision(
            symbol="TST2", direction=Direction.short, reason=DecisionReason.model,
            confidence=0.65, features={k: 0.0 for k in FEATURE_ORDER},
            horizon_hours=4, created_at=T0.replace(tzinfo=None), entry_price=50.0,
        ))
    record_outcomes(model=OnlineModel())
    with db_session() as s:
        d = s.query(Decision).filter_by(symbol="TST2").one()
        assert d.realized_return > 0           # цена упала — шорт в плюсе


def test_copy_decision_does_not_crash_learning(no_model_save):
    """Признаки copy-решений другие — фиксация исхода не должна падать."""
    with db_session() as s:
        _seed_bars(s, "TST3", T0 - timedelta(hours=1), [200])
        _seed_bars(s, "TST3", T0 + timedelta(hours=1), [201, 202, 203, 204, 205])
        s.add(Decision(
            symbol="TST3", direction=Direction.long, reason=DecisionReason.copy,
            confidence=0.6, features={"insider": "Test Person", "code": "P", "value_usd": 1e6},
            horizon_hours=4, created_at=T0.replace(tzinfo=None), entry_price=200.0,
        ))
    model = OnlineModel()
    n_before = model.n_seen
    record_outcomes(model=model)               # раньше падало с KeyError
    assert model.n_seen == n_before            # copy не учит модель направления
    with db_session() as s:
        d = s.query(Decision).filter_by(symbol="TST3").one()
        assert d.realized_return is not None   # но исход зафиксирован


def test_model_learns_from_regular_decision(no_model_save):
    with db_session() as s:
        _seed_bars(s, "TST4", T0 - timedelta(hours=1), [10])
        _seed_bars(s, "TST4", T0 + timedelta(hours=1), [10, 11, 12, 13, 14])
        s.add(Decision(
            symbol="TST4", direction=Direction.flat, reason=DecisionReason.model,
            confidence=0.5, features={k: 0.1 for k in FEATURE_ORDER},
            horizon_hours=4, created_at=T0.replace(tzinfo=None), entry_price=10.0,
        ))
    model = OnlineModel()
    record_outcomes(model=model)
    assert model.n_seen == 1                   # flat тоже обучает (метка = рынок)
