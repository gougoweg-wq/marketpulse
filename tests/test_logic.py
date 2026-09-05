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
from marketpulse.config import settings  # noqa: E402

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
    monkeypatch.setattr(OnlineModel, "save", lambda self, session=None: None)


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


def test_features_accept_timezone_aware_bars():
    """Postgres отдаёт tz-aware даты — признаки не должны падать на сравнении."""
    from marketpulse.model.features import build_features
    from marketpulse.db.models import NewsCluster
    aware_start = T0 - timedelta(hours=40)
    bars = [PriceBar(symbol="TZ", interval="1h", ts=aware_start + timedelta(hours=i),
                     open=10, high=10, low=10, close=10 + i * 0.01, volume=100)
            for i in range(40)]
    cluster = NewsCluster(representative_title="t", first_seen_at=T0, n_articles=1,
                          n_sources=1, tickers=["TZ"], sentiment=0.3)
    feats = build_features(cluster, "TZ", T0, ctx={"bars": {"TZ": bars}, "clusters": [cluster]})
    assert feats is not None and "ret_24h" in feats


def test_executor_caps_concentration_per_symbol(monkeypatch):
    """Три сигнала по одному тикеру в один тик не должны собрать 15% капитала в нём."""
    from marketpulse.trading import executor
    from marketpulse.db.models import Trade, TradeStatus

    monkeypatch.setattr(executor, "_alpaca_client", lambda: None)
    monkeypatch.setattr(executor, "_market_open", lambda s, c: True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db_session() as s:
        for i in range(3):
            s.add(Decision(
                symbol="CONC", direction=Direction.long, reason=DecisionReason.model,
                confidence=0.75, features={k: 0.0 for k in FEATURE_ORDER},
                horizon_hours=4, created_at=now, entry_price=100.0,
            ))
    executor.execute_new_decisions()
    with db_session() as s:
        opened = s.query(Trade).filter_by(symbol="CONC", status=TradeStatus.filled).all()
        total = sum(t.notional for t in opened)
    # лимит: 2 × max_position_pct от капитала
    assert total <= executor.STARTING_EQUITY * settings.max_position_pct * 2 + 1e-6
    assert len(opened) < 3



def test_executor_skips_when_market_closed(monkeypatch):
    """При закрытом рынке сделки не открываются, решение остаётся без сделки."""
    from marketpulse.trading import executor
    from marketpulse.db.models import Trade

    monkeypatch.setattr(executor, "_alpaca_client", lambda: None)
    monkeypatch.setattr(executor, "_market_open", lambda s, c: False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db_session() as s:
        s.add(Decision(
            symbol="CLSD", direction=Direction.long, reason=DecisionReason.model,
            confidence=0.8, features={k: 0.0 for k in FEATURE_ORDER},
            horizon_hours=4, created_at=now, entry_price=50.0,
        ))
    executor.execute_new_decisions()
    with db_session() as s:
        assert s.query(Trade).filter_by(symbol="CLSD").count() == 0


def test_executor_ignores_stale_decisions(monkeypatch):
    """Решение старше одного тика (риск-лимит отложил) не исполняется задним числом."""
    from marketpulse.trading import executor
    from marketpulse.db.models import Trade

    monkeypatch.setattr(executor, "_alpaca_client", lambda: None)
    monkeypatch.setattr(executor, "_market_open", lambda s, c: True)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)
    with db_session() as s:
        s.add(Decision(
            symbol="STALE", direction=Direction.short, reason=DecisionReason.model,
            confidence=0.8, features={k: 0.0 for k in FEATURE_ORDER},
            horizon_hours=4, created_at=old, entry_price=50.0,
        ))
    executor.execute_new_decisions()
    with db_session() as s:
        assert s.query(Trade).filter_by(symbol="STALE").count() == 0


def test_same_bar_entry_exit_is_voided(no_model_save):
    """Вход и выход на одном баре — сделки не было: 0 без издержек, модель не учится."""
    with db_session() as s:
        # единственный бар после решения — и он же «выход» по fallback
        _seed_bars(s, "SAME", T0 + timedelta(hours=1), [10.0])
        s.add(Decision(
            symbol="SAME", direction=Direction.long, reason=DecisionReason.model,
            confidence=0.7, features={k: 0.0 for k in FEATURE_ORDER},
            horizon_hours=4, created_at=T0.replace(tzinfo=None), entry_price=10.0,
        ))
    model = OnlineModel()
    r = record_outcomes(model=model)
    assert r["voided"] >= 1
    assert model.n_seen == 0
    with db_session() as s:
        d = s.query(Decision).filter_by(symbol="SAME").one()
        assert d.realized_return == 0.0 and d.outcome_recorded_at is not None


def test_tickers_ignore_homonyms():
    assert "V" not in extract_tickers("US tightens visa rules for students")
    assert "V" in extract_tickers("Visa Inc reports record quarter")
    assert "JNJ" not in extract_tickers("Boris Johnson resigns")
    assert "JNJ" in extract_tickers("Johnson & Johnson settles talc lawsuit")
    assert "GLD" not in extract_tickers("She won a gold medal in Paris")
    assert "GLD" in extract_tickers("Gold prices hit record high")


def test_asset_classes_and_broker_symbols():
    from marketpulse.assets import alpaca_symbol, is_crypto, normalize_symbol
    assert is_crypto("BTC-USD") and not is_crypto("AAPL")
    assert alpaca_symbol("BTC-USD") == "BTC/USD" and alpaca_symbol("AAPL") == "AAPL"
    # позиции брокера приходят как BTCUSD — сверка должна их узнавать
    assert normalize_symbol("BTCUSD") == normalize_symbol("BTC/USD") == normalize_symbol("BTC-USD")


def test_crypto_tickers_only_by_name():
    assert "BTC-USD" in extract_tickers("Bitcoin jumps above $120,000 as ETF inflows surge")
    assert "ETH-USD" in extract_tickers("Ethereum upgrade goes live")
    assert "SOL-USD" not in extract_tickers("The sol de Mayo appears on the flag")


def test_crypto_trades_when_equity_market_closed(monkeypatch):
    """Крипта торгуется 24/7: при закрытом рынке акций её сделки открываются, акции — нет."""
    from marketpulse.trading import executor
    from marketpulse.db.models import Trade, TradeStatus

    monkeypatch.setattr(executor, "_alpaca_client", lambda: None)
    monkeypatch.setattr(executor, "_market_open", lambda s, c: False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db_session() as s:
        for sym in ("BTC-USD", "MSFT"):
            s.add(Decision(
                symbol=sym, direction=Direction.long, reason=DecisionReason.model,
                confidence=0.8, features={k: 0.0 for k in FEATURE_ORDER},
                horizon_hours=4, created_at=now, entry_price=100.0,
            ))
    executor.execute_new_decisions()
    with db_session() as s:
        assert s.query(Trade).filter_by(symbol="BTC-USD", status=TradeStatus.filled).count() == 1
        assert s.query(Trade).filter_by(symbol="MSFT").count() == 0
