"""Soulseek search text from artist and album names."""

from __future__ import annotations

import re
import unicodedata

# Letters NFKD does not decompose into base + combining mark.
_FOLD_TABLE = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
    "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "þ": "th", "Þ": "TH", "ð": "d", "Ð": "D",
    "ı": "i",
})

_BRACKETED = re.compile(r"\s*[(\[{][^)\]}]*[)\]}]")
_APOSTROPHES = re.compile(r"['’‘`´]")
_NON_WORD = re.compile(r"[^a-z0-9]+")

STOPWORDS = {
    "a", "an", "and", "the", "of", "in", "on", "to", "for", "with", "by", "at", "from", "is", "or",
    "de", "la", "le", "les", "el", "los", "das", "der", "die", "und", "y", "et",
    "vol", "volume", "part", "pt", "edition", "deluxe", "remastered", "remaster", "expanded",
}


def fold(text: str) -> str:
    """Fold diacritics: 'Björk' -> 'Bjork', 'Sigur Rós' -> 'Sigur Ros'."""
    text = text.translate(_FOLD_TABLE)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Lower-case, fold diacritics, strip bracketed suffixes and punctuation, drop a leading 'the'."""
    text = _BRACKETED.sub(" ", fold(text or ""))
    text = text.replace("&", " ")
    text = _APOSTROPHES.sub("", text.lower())
    text = _NON_WORD.sub(" ", text).strip()
    if text.startswith("the ") and len(text) > 4:
        text = text[4:]
    return text


def distinctive_words(normalized: str) -> list[str]:
    return [w for w in normalized.split() if len(w) >= 3 and w not in STOPWORDS]


def primary_query(artist: str, album: str) -> str:
    return " ".join(part for part in (normalize(artist), normalize(album)) if part)


def fallback_queries(artist: str, album: str) -> list[str]:
    """Up to two broader queries, used when the primary finds too few FLAC folders."""
    primary = primary_query(artist, album)
    norm_artist, norm_album = normalize(artist), normalize(album)
    words = distinctive_words(norm_album)
    out: list[str] = []

    # Album title alone, when it is distinctive enough not to drown in noise.
    if len(words) >= 2:
        out.append(norm_album)

    # Artist plus the album's first strong word.
    strong = next((w for w in words if len(w) >= 4), words[0] if words else None)
    if strong and norm_artist:
        out.append(f"{norm_artist} {strong}")

    deduped: list[str] = []
    for q in out:
        if q and q != primary and q not in deduped:
            deduped.append(q)
    return deduped[:2]
