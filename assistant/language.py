"""Cheap language hint (uz / ru / en) for the latest incoming message.

This is only a hint for Claude; Claude makes the final call.
"""

from __future__ import annotations

import re
from typing import Optional

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[a-zA-Z]")

_UZ_CYRILLIC_LETTERS = re.compile(r"[ўқғҳЎҚҒҲ]")
_UZ_CYRILLIC_WORDS = {
    "салом", "ассалому", "алейкум", "алайкум", "рахмат", "раҳмат", "яхши", "нима",
    "қалесан", "калесан", "қалайсан", "калайсан", "йўқ", "йук", "бугун", "эртага",
    "керак", "хоп", "бопти", "зўр", "зур", "ҳозир", "хозир", "кейин", "мен", "сен",
    "сиз", "қаерда", "каерда", "нега", "қачон", "качон",
}

_APOSTROPHES = "'ʻ’‘`"
_APOS_CLASS = "[" + re.escape(_APOSTROPHES) + "]"
# o' / g' as in "bo'sh", "g'alaba", "sog'" - but not English "dog's".
_UZ_OG = re.compile(r"[og]" + _APOS_CLASS + r"(?!s\b)", re.IGNORECASE)
_ENGLISH_APOSTROPHE_WORDS = re.compile(r"\bo" + _APOS_CLASS + r"clock\b", re.IGNORECASE)

_UZ_LATIN_WORDS = {
    "salom", "assalomu", "alaykum", "rahmat", "raxmat", "yaxshi", "nima", "qalesan",
    "qalaysan", "qayerda", "qayerdasan", "bugun", "ertaga", "kerak", "xop", "bopti",
    "zor", "hozir", "keyin", "men", "sen", "siz", "yoq", "nega", "qachon", "qanday",
    "bormi", "yoqmi", "kechirasiz", "iltimos", "albatta", "mayli",
}
_UZ_SUFFIXES = (
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti", "yapmiz", "asizmi",
    "ganman", "dingmi",
)
_LATIN_WORD = re.compile(r"[a-z" + re.escape(_APOSTROPHES) + r"]+")


def detect_language(text: str) -> Optional[str]:
    """Return "uz", "ru", "en", or None when the text has no letters."""
    text = text or ""
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    if not cyrillic and not latin:
        return None

    if cyrillic > latin:
        if _UZ_CYRILLIC_LETTERS.search(text):
            return "uz"
        words = re.findall(r"[Ѐ-ӿ]+", text.lower())
        return "uz" if any(word in _UZ_CYRILLIC_WORDS for word in words) else "ru"

    lowered = _ENGLISH_APOSTROPHE_WORDS.sub(" ", text.lower())
    if _UZ_OG.search(lowered):
        return "uz"
    for raw in _LATIN_WORD.findall(lowered):
        word = raw.strip(_APOSTROPHES)
        bare = "".join(ch for ch in word if ch not in _APOSTROPHES)
        if bare in _UZ_LATIN_WORDS:
            return "uz"
        if any(bare.endswith(suffix) and len(bare) > len(suffix) for suffix in _UZ_SUFFIXES):
            return "uz"
    return "en"
