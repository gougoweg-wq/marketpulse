"""Онлайн-обучающаяся модель.

Логистическая регрессия с partial_fit (SGD): учится инкрементально
на каждом закрытом решении. Предсказывает p(цена вырастет за горизонт).

Направление сделки:
  p > 0.5 + порог  -> long
  p < 0.5 - порог  -> short   (шорты — равноправное направление)

Контрарианский слой: если толпа на экстремуме (crowd_extreme=1)
и модель не уверена, голосуем ПРОТИВ толпы — исторически экстремум
хайпа чаще разворот, чем продолжение.

Исследование (exploration_rate): часть решений принимается случайно,
иначе модель никогда не узнает исходов действий, которые сама не выбрала бы.
"""
from __future__ import annotations

import pickle
import random
from pathlib import Path

import numpy as np
from sklearn.linear_model import SGDClassifier

from marketpulse.config import settings
from marketpulse.db.models import Direction, DecisionReason
from marketpulse.model.features import FEATURE_ORDER, to_vector

MODEL_PATH = Path(settings.log_dir).parent / "data" / "model.pkl"


class OnlineModel:
    def __init__(self) -> None:
        self.clf = SGDClassifier(
            loss="log_loss", alpha=1e-4, learning_rate="constant", eta0=0.01,
            random_state=42,
        )
        self.n_seen = 0
        self.version = "v1"

    # --- persist ---

    @classmethod
    def load(cls) -> "OnlineModel":
        # приоритет — БД: единственное хранилище, живущее в облаке между запусками
        from marketpulse.db.models import ModelBlob
        from marketpulse.db.session import db_session

        from sqlalchemy.exc import OperationalError, ProgrammingError

        try:
            with db_session() as s:
                blob = s.get(ModelBlob, 1)
                if blob is not None:
                    return pickle.loads(blob.data)
        except (OperationalError, ProgrammingError) as exc:
            # только "таблицы ещё нет" — любой другой сбой базы должен уронить тик,
            # иначе свежая пустая модель затрёт обученную при следующем save()
            if "model_blob" not in str(exc):
                raise
        if MODEL_PATH.exists():
            with open(MODEL_PATH, "rb") as f:
                return pickle.load(f)
        return cls()

    def save(self) -> None:
        from datetime import datetime, timezone

        from marketpulse.db.models import ModelBlob
        from marketpulse.db.session import db_session

        payload = pickle.dumps(self)
        with db_session() as s:
            blob = s.get(ModelBlob, 1)
            if blob is None:
                blob = ModelBlob(id=1, data=payload, version=self.version)
                s.add(blob)
            else:
                blob.data = payload
                blob.version = self.version
                blob.updated_at = datetime.now(timezone.utc)
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(self, f)

    # --- learn ---

    def learn_one(self, features: dict, went_up: bool) -> None:
        x = np.array([to_vector(features)])
        y = np.array([1 if went_up else 0])
        self.clf.partial_fit(x, y, classes=np.array([0, 1]))
        self.n_seen += 1
        self.version = f"v1.{self.n_seen}"

    # --- decide ---

    def predict_up_proba(self, features: dict) -> float:
        if self.n_seen < 10:
            return 0.5  # холодный старт: модель ещё ничего не знает
        x = np.array([to_vector(features)])
        return float(self.clf.predict_proba(x)[0, 1])

    def decide(self, features: dict) -> tuple[Direction, float, DecisionReason]:
        """(направление, уверенность, причина)."""
        # исследовательская сделка: случайное направление, малый размер
        if random.random() < settings.exploration_rate:
            d = random.choice([Direction.long, Direction.short])
            return d, 0.5, DecisionReason.exploration

        p_up = self.predict_up_proba(features)
        edge = settings.min_confidence - 0.5

        # контрарианский слой: толпа на экстремуме, модель без сильного мнения
        if (
            settings.contrarian_enabled
            and features.get("crowd_extreme") == 1.0
            and abs(p_up - 0.5) < edge
        ):
            crowd = features.get("crowd_sentiment", 0.0)
            d = Direction.short if crowd > 0 else Direction.long
            conf = 0.5 + min(abs(crowd), 0.9) * edge  # уверенность от силы экстремума
            return d, conf, DecisionReason.contrarian

        if p_up >= settings.min_confidence:
            return Direction.long, p_up, DecisionReason.model
        if p_up <= 1 - settings.min_confidence:
            return Direction.short, 1 - p_up, DecisionReason.model
        return Direction.flat, max(p_up, 1 - p_up), DecisionReason.model
