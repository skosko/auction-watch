"""Shared utilities for all scrapers."""

import re
import unicodedata

# ISO 4217 → display symbol. Falls back to the code itself if missing.
CURRENCY_SYMBOL: dict[str, str] = {
    "USD": "$", "GBP": "£", "EUR": "€", "HKD": "HK$",
    "CHF": "CHF ", "JPY": "¥", "AUD": "A$", "CAD": "C$",
}


def normalize(s: str) -> str:
    """Lowercase + strip diacritics for forgiving artist-name matching."""
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def currency_symbol(code: str | None) -> str | None:
    """Return display symbol for an ISO currency code, the code itself, or None."""
    if not code:
        return None
    return CURRENCY_SYMBOL.get(code.upper(), code) or None


# ── Dimension extraction ───────────────────────────────────────────────────
# Shared across scrapers that parse free-text lot descriptions.

# N x N [x N] cm/in/mm — handles comma as decimal separator
_DIM_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)"
    r"(?:\s*[xX×]\s*[\d.,]+)?\s*(cm|in(?:ch(?:es)?)?|mm)\b",
    re.IGNORECASE,
)

# French H/L labelled format: "H: 19; L: 26 cm" or "Hauteur: 50, Largeur: 70 cm"
_DIM_HL_RE = re.compile(
    r"[Hh](?:auteur)?[.:]?\s*(\d+(?:[.,]\d+)?)\s*(?:cm|in|mm)?\s*[;,]?\s*"
    r"[Ll](?:argeur)?[.:]?\s*(\d+(?:[.,]\d+)?)\s*(cm|in(?:ch(?:es)?)?|mm)\b",
    re.IGNORECASE,
)


def _to_cm(a: float, b: float, unit: str) -> str:
    u = unit.lower()
    if u == "mm":
        a, b = round(a / 10, 1), round(b / 10, 1)
    elif u.startswith("in"):
        a, b = round(a * 2.54, 1), round(b * 2.54, 1)
    return f"{a:g} × {b:g} cm"


def extract_dimensions(text: str) -> str | None:
    """Parse first H×W dimension from a lot description, normalised to cm."""
    m = _DIM_RE.search(text)
    if m:
        try:
            w, h = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        except ValueError:
            return None
        return _to_cm(w, h, m.group(3))
    m = _DIM_HL_RE.search(text)
    if m:
        try:
            h_val, l_val = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        except ValueError:
            return None
        return _to_cm(h_val, l_val, m.group(3))
    return None
