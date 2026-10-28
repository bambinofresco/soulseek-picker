"""Edition preferences: which pressing to favour when several folders match equally well.

The tiers stay the contract (Exact beats High whatever the preferences say); within a
tier these scores order the rows, pick the edition the confirm form preselects, and feed
the recommendation. Everything is additive and explained: every point comes with a reason
("+ GB", "− remaster") that the row shows on hover.

Settings (Settings → Preferences), all optional:
    avoid_remasters      editions and folders that say remaster rank below everything else
    preferred_countries  ordered, e.g. "GB, US, XE, JP"; XW (worldwide) counts as the first
    preferred_sources    ordered folder sources / edition formats, e.g. "CD, Vinyl, WEB"
    preferred_depth      "16", "24" or "any"
    avoid_flags          edition markers to push down, e.g. "Deluxe, Anniversary"
    prefer_flags         markers to pull up, e.g. "MFSL, Japan"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Settings
from .editions import Edition, REMASTER_RE

REMASTER_PENALTY = 10
REMASTER_MAYBE_PENALTY = 3  # some of the folder's possible editions are remasters
FLAG_POINTS = 3
DEPTH_POINTS = 2
COUNTRY_STEP = 1  # first preferred country gets len(list) points, the next one fewer, ...
SOURCE_STEP = 1


@dataclass
class PrefScore:
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, points: int, reason: str) -> None:
        if points:
            self.score += points
            self.reasons.append(f"{'+' if points > 0 else '−'} {reason}")

    @property
    def label(self) -> str:
        return f"{self.score:+d}" if self.score else "0"


def _list(text: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[,\s]+", text or "") if t.strip()]


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)}


def _flag_hits(wanted: list[str], haystack: list[str]) -> list[str]:
    """Which of the wanted markers appear in the folder's flags/territories or an edition's words."""
    have = {h.lower() for h in haystack}
    return [w for w in wanted if w.lower() in have]


def _rank_points(value: str | None, ordered: list[str], step: int, wildcard: str | None = None) -> tuple[int, str | None]:
    """len(list) points for the first entry, one fewer for each later one, 0 when absent."""
    if not value or not ordered:
        return 0, None
    upper = [o.upper() for o in ordered]
    v = value.upper()
    if wildcard and v == wildcard:
        return len(ordered) * step, f"{value} (any country)"
    if v in upper:
        return (len(ordered) - upper.index(v)) * step, value
    return 0, None


def edition_words(ed: Edition) -> list[str]:
    """Markers an edition's title, disambiguation and formats carry, for the flag settings."""
    words = _words(f"{ed.title} {ed.disambiguation} {' '.join(ed.formats)}")
    words |= {f.lower() for f in ed.families}
    if ed.country:
        words.add(ed.country.lower())
    return sorted(words)


def score_edition(ed: Edition, settings: Settings) -> PrefScore:
    """An edition on its own (used to preselect among a folder's possible editions)."""
    s = PrefScore()
    if settings.avoid_remasters and ed.is_remaster:
        s.add(-REMASTER_PENALTY, "remaster")
    points, reason = _rank_points(ed.country, _list(settings.preferred_countries), COUNTRY_STEP, "XW")
    if reason:
        s.add(points, reason)
    best = max(((pts, r) for fam in ed.families for pts, r in [_rank_points(fam, _list(settings.preferred_sources), SOURCE_STEP)]),
               default=(0, None))
    if best[1]:
        s.add(best[0], best[1])
    words = edition_words(ed)
    for hit in _flag_hits(_list(settings.avoid_flags), words):
        s.add(-FLAG_POINTS, hit)
    for hit in _flag_hits(_list(settings.prefer_flags), words):
        s.add(FLAG_POINTS, hit)
    return s


def best_edition(candidates: list[Edition], settings: Settings) -> Edition | None:
    """The preferred one among editions a folder could be: highest score, then earliest date."""
    if not candidates:
        return None
    return max(candidates, key=lambda e: (score_edition(e, settings).score, -(int(e.date[:4]) if e.date[:4].isdigit() else 9999)))


def score_candidate(parsed: dict, depth: int | None, matched: list[Edition], settings: Settings) -> PrefScore:
    """A folder: what its name says plus the best of the editions it could be."""
    s = PrefScore()
    remaster_folder = parsed.get("remaster") == "folder"
    if settings.avoid_remasters:
        if remaster_folder or (matched and all(e.is_remaster for e in matched)):
            s.add(-REMASTER_PENALTY, "remaster")
        elif any(e.is_remaster for e in matched):
            s.add(-REMASTER_MAYBE_PENALTY, "may be a remaster")
    countries = _list(settings.preferred_countries)
    if countries:
        options = [e.country for e in matched if e.country] or list(parsed.get("territories") or [])
        best = max((_rank_points(c, countries, COUNTRY_STEP, "XW") for c in options), default=(0, None))
        if best[1]:
            s.add(best[0], best[1])
    sources = _list(settings.preferred_sources)
    if sources:
        source = parsed.get("source")
        options = [source] if source else [fam for e in matched for fam in e.families]
        best = max((_rank_points(o, sources, SOURCE_STEP) for o in options), default=(0, None))
        if best[1]:
            s.add(best[0], best[1])
    want = (settings.preferred_depth or "any").lower()
    if want in ("16", "24") and depth:
        s.add(DEPTH_POINTS if str(depth) == want else -DEPTH_POINTS, f"{depth}-bit")
    words = [*(parsed.get("flags") or []), *(parsed.get("territories") or [])]
    if matched:
        words += [w for e in matched for w in edition_words(e)]
    for hit in _flag_hits(_list(settings.avoid_flags), words):
        s.add(-FLAG_POINTS, hit)
    for hit in _flag_hits(_list(settings.prefer_flags), words):
        s.add(FLAG_POINTS, hit)
    return s


def is_remaster_text(text: str) -> bool:
    return bool(REMASTER_RE.search(text or ""))
