"""US-only symbol parser and classifier for US equities, ETFs, Treasury ETFs, and CME futures."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, FrozenSet

US_EXCHANGE_PREFIXES: FrozenSet[str] = frozenset(
    {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "IEX", "CME", "CBOT", "GLOBEX"}
)

US_FUTURES_ROOTS: FrozenSet[str] = frozenset(
    {"ES", "NQ", "YM", "RTY", "MES", "MNQ", "MYM", "M2K", "ZB", "ZN", "ZF", "ZT"}
)

US_BOND_ETFS: FrozenSet[str] = frozenset(
    {"TLT", "IEF", "SHY", "AGG", "BND", "VGIT", "VGLT", "GOVT", "EDV", "TMF", "BIL", "SGOV", "LQD", "TIP"}
)

US_EQUITY_ETFS: FrozenSet[str] = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "VOO",
        "VTI",
        "IVV",
        "XLK",
        "XLF",
        "XLE",
        "XLV",
        "XLI",
        "XLY",
        "XLP",
        "XLU",
        "XLB",
        "XLRE",
        "XLC",
        "SMH",
        "SOXX",
        "ARKK",
        "GLD",
        "SLV",
    }
)

_CLASS_SHARE_RE = re.compile(r"^([A-Z]{1,5})[\./]([A-Z])$")
_VALID_EQUITY_RE = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}$")
_VALID_FUTURES_RE = re.compile(r"^([A-Z0-9]{1,5})=F$")
_CME_MONTH_CONTRACT_RE = re.compile(r"^(ES|NQ|YM|RTY|MES|MNQ|MYM|M2K|ZB|ZN)[FGHJKMNQUVXZ]\d{1,2}$")


@dataclass(frozen=True)
class UsSymbol:
    """Normalized US symbol representation."""

    code: str
    raw: str
    exchange: str = ""
    is_futures: bool = False
    is_bond: bool = False
    is_etf: bool = False

    @property
    def is_equity(self) -> bool:
        return not self.is_futures

    @property
    def yf_ticker(self) -> str:
        """Yahoo Finance compatible ticker (e.g., BRK-B, ES=F, NVDA)."""
        return self.code

    @classmethod
    def parse(cls, raw: Any) -> "UsSymbol":
        if raw is None or isinstance(raw, bool) or not isinstance(raw, str):
            raise ValueError(f"Invalid US symbol: {raw!r}")
        cleaned = raw.strip().upper()
        if not cleaned:
            raise ValueError("Empty US symbol")

        exchange = ""
        if ":" in cleaned:
            pfx, rest = cleaned.split(":", 1)
            pfx = pfx.strip()
            rest = rest.strip()
            if pfx not in US_EXCHANGE_PREFIXES or not rest:
                raise ValueError(f"Unsupported exchange prefix in symbol: {raw!r}")
            exchange = pfx
            cleaned = rest

        # CME slash notation e.g. /ES -> ES=F, /NQ -> NQ=F
        if cleaned.startswith("/"):
            root = cleaned[1:].strip()
            if root in US_FUTURES_ROOTS:
                return cls(
                    code=f"{root}=F",
                    raw=raw,
                    exchange=exchange or "CME",
                    is_futures=True,
                    is_bond=root in ("ZB", "ZN", "ZF", "ZT"),
                    is_etf=False,
                )
            raise ValueError(f"Unsupported CME futures root: {raw!r}")

        m_fut = _VALID_FUTURES_RE.match(cleaned)
        if m_fut:
            root = m_fut.group(1)
            return cls(
                code=cleaned,
                raw=raw,
                exchange=exchange or "CME",
                is_futures=True,
                is_bond=root in ("ZB", "ZN", "ZF", "ZT"),
                is_etf=False,
            )

        m_contract = _CME_MONTH_CONTRACT_RE.match(cleaned)
        if m_contract:
            root = m_contract.group(1)
            return cls(
                code=cleaned,
                raw=raw,
                exchange=exchange or "CME",
                is_futures=True,
                is_bond=root in ("ZB", "ZN"),
                is_etf=False,
            )

        # Class shares: BRK.B / BRK/B -> BRK-B
        m_cls = _CLASS_SHARE_RE.match(cleaned)
        if m_cls:
            cleaned = f"{m_cls.group(1)}-{m_cls.group(2)}"

        if not _VALID_EQUITY_RE.match(cleaned):
            raise ValueError(f"Unsupported or non-US symbol format: {raw!r}")

        is_bond = cleaned in US_BOND_ETFS
        is_etf = is_bond or (cleaned in US_EQUITY_ETFS)
        return cls(
            code=cleaned,
            raw=raw,
            exchange=exchange,
            is_futures=False,
            is_bond=is_bond,
            is_etf=is_etf,
        )


def normalize_us_symbol(raw: Any) -> str:
    """Return canonical uppercase US symbol string (e.g. 'NASDAQ:NVDA' -> 'NVDA', 'NYSE:BRK.B' -> 'BRK-B')."""
    return UsSymbol.parse(raw).code


def is_us_futures_symbol(raw: Any) -> bool:
    """Return True if raw symbol parses as a CME US futures contract (e.g. ES=F, NQ=F)."""
    if not raw or isinstance(raw, bool) or not isinstance(raw, str):
        return False
    try:
        return UsSymbol.parse(raw).is_futures
    except ValueError:
        return False
