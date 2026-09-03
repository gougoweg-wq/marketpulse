"""Настройки приложения. Все значения можно переопределить через .env."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- база данных ---
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'marketpulse.db'}"  # env: DATABASE_URL

    # --- сбор новостей ---
    fetch_concurrency: int = 20          # одновременных запросов к источникам
    fetch_timeout_sec: int = 15
    fetch_retries: int = 2
    user_agent: str = "MarketPulse/0.1 (research project)"

    # --- телеграм (публичные каналы через t.me/s/) ---
    telegram_enabled: bool = True

    # --- рынок ---
    watchlist: list[str] = [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
        "AMD", "INTC", "NFLX", "JPM", "GS", "XOM", "CVX", "KO",
        "PFE", "JNJ", "BA", "DIS", "V", "MA", "PYPL", "COIN",
        "GLD", "SLV", "USO", "SPY", "QQQ", "IWM", "TLT",
    ]
    price_bar_interval: str = "1h"       # интервал баров для обучения

    # --- модель ---
    prediction_horizon_hours: int = 4    # на сколько вперёд предсказываем
    exploration_rate: float = 0.10       # доля исследовательских решений
    retrain_every_hours: int = 24

    # --- риск ---
    max_position_pct: float = 0.05       # максимум 5% счёта на позицию
    max_gross_exposure: float = 1.0      # суммарная экспозиция не выше 100%
    min_confidence: float = 0.58         # ниже — не торгуем
    stop_loss_pct: float = 0.03

    # --- контрарианский модуль ---
    contrarian_enabled: bool = True
    sentiment_extreme_pct: float = 0.90  # квантиль «толпа на экстремуме»

    # --- Alpaca paper ---
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True            # только демо-счёт, реальная торговля выключена

    # --- логи ---
    log_dir: Path = PROJECT_ROOT / "logs"


settings = Settings()
