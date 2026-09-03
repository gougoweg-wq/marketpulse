"""Копитрейдинг «умных денег»: сделки инсайдеров из SEC Form 4.

Инсайдеры (директора, топ-менеджеры, владельцы 10%+) обязаны раскрывать
сделки со своими акциями в течение 2 рабочих дней — форма 4 на EDGAR.
Это единственный легальный поток «сделок лучших трейдеров» в реальном
времени: люди, знающие компанию изнутри, торгуют её акциями.

Пайплайн: лента свежих Form 4 -> фильтр по вотчлисту -> разбор XML
(код сделки, объём, цена) -> сигнал копирования на демо-счёт.
Покупка инсайдера (код P) — сильный сигнал long: покупают только
за свои и только веря в рост. Продажа (S) — слабый сигнал short:
продают по тысяче причин (налоги, диверсификация).
"""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import aiohttp
from sqlalchemy import select

from marketpulse.config import settings
from marketpulse.db.models import (
    Decision, DecisionReason, Direction, InsiderFiling, LogEntry, PriceBar,
)
from marketpulse.db.session import db_session

log = logging.getLogger("smartmoney")

# SEC требует представляться контактом в User-Agent
SEC_HEADERS = {"User-Agent": "MarketPulse research maksim130874@gmail.com"}
MAX_DOC_FETCHES = 12  # вежливый лимит на один проход

_TITLE_RE = re.compile(r"^4(?:/A)?\s+-\s+(.+?)\s+\((\d{10})\)\s+\((Issuer|Reporting)\)")
_XML_LINK_RE = re.compile(r'href="(/Archives/[^"]+\.xml)"', re.I)

# имена компаний вотчлиста в форме, встречающейся в EDGAR
COMPANY_TO_SYMBOL = {
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA",
    "alphabet": "GOOGL", "amazon": "AMZN", "meta platforms": "META",
    "tesla": "TSLA", "advanced micro": "AMD", "intel": "INTC",
    "netflix": "NFLX", "jpmorgan": "JPM", "goldman sachs": "GS",
    "exxon": "XOM", "chevron": "CVX", "coca-cola": "KO", "coca cola": "KO",
    "pfizer": "PFE", "johnson & johnson": "JNJ", "boeing": "BA",
    "walt disney": "DIS", "visa": "V", "mastercard": "MA",
    "paypal": "PYPL", "coinbase": "COIN",
}


def _match_symbol(company: str) -> str | None:
    lo = company.lower()
    for name, sym in COMPANY_TO_SYMBOL.items():
        if name in lo:
            return sym
    return None


def _parse_form4_xml(raw: bytes) -> dict | None:
    """Достаёт из XML формы 4 суть сделки."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    def txt(path: str) -> str | None:
        el = root.find(path)
        return el.text.strip() if el is not None and el.text else None

    symbol = txt(".//issuerTradingSymbol")
    owner = txt(".//rptOwnerName")
    code = None
    shares = price = None
    # берём первую несервисную транзакцию
    for tr in root.iter("nonDerivativeTransaction"):
        code = tr.findtext(".//transactionCode")
        sh = tr.findtext(".//transactionShares/value")
        pr = tr.findtext(".//transactionPricePerShare/value")
        shares = float(sh) if sh else None
        price = float(pr) if pr else None
        if code:
            break
    if not code:
        return None
    return {"symbol": symbol, "insider_name": owner, "transaction_code": code,
            "shares": shares, "price": price}


async def _company_form4(http: aiohttp.ClientSession, symbol: str) -> list[tuple[str, str, str]]:
    """Свежие Form 4 компании: [(accession, href, updated)]. Тикер = CIK-параметр EDGAR."""
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={symbol}&type=4&dateb=&owner=include&count=10&output=atom"
    )
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            atom = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", atom, re.S):
        acc = re.search(r"accession-n?u?m?b?e?r=([0-9-]+)", entry)
        href = re.search(r'href="([^"]+-index\.htm[^"]*)"', entry)
        upd = re.search(r"<updated>(.*?)</updated>", entry)
        ftype = re.search(r"<filing-type>(.*?)</filing-type>", entry)
        if acc and href and (not ftype or ftype.group(1).startswith("4")):
            out.append((acc.group(1), href.group(1), upd.group(1) if upd else ""))
    return out


async def fetch_insiders() -> dict:
    """Адресный обход Form 4 по всем компаниям вотчлиста."""
    symbols = [s for s in settings.watchlist if s in COMPANY_TO_SYMBOL.values()]
    added = 0
    fetched_docs = 0

    with db_session() as s:
        known = {r for r in s.execute(select(InsiderFiling.accession)).scalars()}

    async with aiohttp.ClientSession(headers=SEC_HEADERS) as http:
        for sym in symbols:
            filings = await _company_form4(http, sym)
            await asyncio.sleep(0.15)  # вежливость к SEC (<10 rps)
            for accession, href, updated in filings:
                if accession in known or fetched_docs >= MAX_DOC_FETCHES:
                    continue
                known.add(accession)
                fetched_docs += 1

                detail: dict | None = None
                try:
                    async with http.get(href, timeout=aiohttp.ClientTimeout(total=15)) as r2:
                        if r2.status == 200:
                            page = await r2.text()
                            links = [l for l in _XML_LINK_RE.findall(page) if "xsl" not in l.lower()]
                            if links:
                                async with http.get(
                                    "https://www.sec.gov" + links[0],
                                    timeout=aiohttp.ClientTimeout(total=15),
                                ) as r3:
                                    if r3.status == 200:
                                        detail = _parse_form4_xml(await r3.read())
                    await asyncio.sleep(0.15)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    detail = None

                try:
                    filed_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except ValueError:
                    filed_at = datetime.now(timezone.utc)

                with db_session() as s:
                    s.add(InsiderFiling(
                        accession=accession, filed_at=filed_at,
                        company=(detail or {}).get("symbol") or sym,
                        symbol=sym,
                        insider_name=(detail or {}).get("insider_name"),
                        transaction_code=(detail or {}).get("transaction_code"),
                        shares=(detail or {}).get("shares"),
                        price=(detail or {}).get("price"),
                        value_usd=(
                            (detail or {}).get("shares", 0) * (detail or {}).get("price", 0)
                            if (detail or {}).get("shares") and (detail or {}).get("price")
                            else None
                        ),
                        url=href,
                    ))
                    added += 1

    with db_session() as s:
        s.add(LogEntry(
            component="smartmoney",
            message=f"EDGAR Form 4: +{added} сделок инсайдеров по вотчлисту",
        ))
    return {"added": added}


def generate_copy_signals() -> dict:
    """Сигналы копирования по некопированным сделкам инсайдеров.

    P (покупка за свои) -> long, уверенность растёт с размером сделки.
    S (продажа)         -> short со слабой уверенностью.
    Остальные коды (опционы, гранты, налоги) — не сигнал.
    """
    created = 0
    with db_session() as s:
        pending = s.execute(
            select(InsiderFiling).where(
                InsiderFiling.copied == 0,
                InsiderFiling.transaction_code.in_(["P", "S"]),
            )
        ).scalars().all()

        for f in pending:
            f.copied = 1
            if f.symbol not in settings.watchlist:
                continue
            bar = s.execute(
                select(PriceBar).where(PriceBar.symbol == f.symbol)
                .order_by(PriceBar.ts.desc()).limit(1)
            ).scalar()
            if bar is None:
                continue

            if f.transaction_code == "P":
                direction = Direction.long
                # сделка на $1M+ — максимум уверенности
                size_frac = min((f.value_usd or 0) / 1_000_000, 1.0)
                conf = 0.60 + 0.15 * size_frac
            else:
                direction = Direction.short
                conf = 0.56  # продажи — слабый сигнал

            s.add(Decision(
                symbol=f.symbol, direction=direction,
                reason=DecisionReason.copy, confidence=conf,
                features={"insider": f.insider_name or "?",
                          "code": f.transaction_code,
                          "value_usd": f.value_usd or 0},
                model_version="copy",
                horizon_hours=settings.prediction_horizon_hours * 6,  # длиннее горизонт
                entry_price=bar.close,
            ))
            created += 1

        if created:
            s.add(LogEntry(
                component="smartmoney",
                message=f"копитрейдинг: создано {created} сигналов по сделкам инсайдеров",
            ))
    return {"created": created}
