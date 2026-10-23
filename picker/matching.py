"""Score a candidate folder against every edition of the album.

The result is a confidence tier plus the set of editions at the top score (the ambiguity
set), never a single forced guess: editions that share a track list and lengths stay
together as "one of these N" until some signal tells them apart.

The tiers are the contract (see the spec); the weights below are only a starting point
and are meant to be tuned against real folders.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .editions import Edition
from .grouping import disc_number, split_path
from .parsing import normalize_catalog

WEIGHTS: dict[str, float] = {
    "disc_id": 100,
    "catalog": 40,
    "barcode": 40,
    "tracks": 10,
    "tracks_mismatch": -20,
    "durations": 20,
    "durations_partial": 5,
    "durations_bad": -10,
    "year": 8,
    "year_miss": -3,
    "label": 8,
    "label_miss": -3,
    "source": 8,
    "source_conflict": -25,
    "remaster": 6,
    "remaster_conflict": -6,
    "remaster_unflagged": -3,
    "markers": 4,
    "territory": 3,
    "territory_miss": -2,
}

TIERS = ["Unknown", "Low", "Medium", "High", "Exact"]  # ascending
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

DURATION_TOLERANCE = 2.0  # seconds
MAX_MISSING_LENGTHS = 0.2  # skip durations when more than this share of tracks lack a length
EUROPEAN = {"XE", "DE", "GB", "FR", "NL", "IT", "ES", "SE", "AT", "BE", "DK", "NO", "FI", "PL", "CZ", "GR", "PT", "IE", "CH"}
MARKER_TEXT = {
    "Deluxe": re.compile(r"deluxe", re.I),
    "Expanded": re.compile(r"expanded", re.I),
    "Anniversary": re.compile(r"anniversary", re.I),
    "Mono": re.compile(r"\bmono\b", re.I),
    "Stereo": re.compile(r"\bstereo\b", re.I),
    "SHM-CD": re.compile(r"shm", re.I),
    "Blu-spec": re.compile(r"blu-?spec", re.I),
    "MFSL": re.compile(r"mobile fidelity|mfsl|ultradisc", re.I),
    "Gold": re.compile(r"\bgold\b", re.I),
    "Promo": re.compile(r"promo", re.I),
}


@dataclass
class Signal:
    name: str
    weight: float
    detail: str


@dataclass
class EditionScore:
    edition_id: str
    tier: str
    score: float
    signals: list[Signal] = field(default_factory=list)
    tracks_agree: bool = False
    media: list[int] = field(default_factory=list)  # medium indexes the folder corresponds to


@dataclass
class MatchResult:
    tier: str = "Unknown"
    editions: list[str] = field(default_factory=list)  # the ambiguity set, best first by date
    score: float = 0.0
    signals: list[Signal] = field(default_factory=list)  # of the first edition in the set
    tracks_agree: bool = False
    earliest_date: str = ""
    track_diffs: list[dict] = field(default_factory=list)  # per track vs the first edition in the set
    matched_catalogs: list[str] = field(default_factory=list)  # parsed tokens that matched

    def to_dict(self) -> dict:
        return asdict(self)


# Candidate side --------------------------------------------------------------------------

def candidate_discs(files: list[dict]) -> list[list[dict]]:
    """Audio files grouped per disc, in the same order grouping.summary counts them."""
    audio = sorted((f for f in files if f.get("kind") == "audio"), key=lambda f: f["filename"].lower())
    discs: dict[str | None, list[dict]] = {}
    for f in audio:
        discs.setdefault(f.get("disc"), []).append(f)
    order = sorted(discs, key=lambda d: (d is not None, disc_number(d) if d else 0, d or ""))
    return [discs[d] for d in order]


def _tracks_agree(counts: list[int], ed: Edition) -> list[int] | None:
    """Medium indexes the folder's discs correspond to, or None when the counts disagree."""
    for media in (list(range(len(ed.formats))), ed.audio_media):
        ed_counts = [ed.track_counts[i] for i in media]
        if not ed_counts:
            continue
        if counts == ed_counts:
            return media
        # One folder holding every disc, or vinyl sides split into folders.
        if sum(counts) == sum(ed_counts) and (len(counts) == 1 or len(ed_counts) == 1):
            return media
    return None


def _durations(cand: list[float | None], ed: list[float | None]) -> tuple[str, float, float] | None:
    """('durations' | 'durations_partial' | 'durations_bad', share within tolerance, mean abs diff)."""
    if not ed or len(cand) != len(ed):
        return None
    if sum(1 for x in ed if x is None) > MAX_MISSING_LENGTHS * len(ed):
        return None
    if sum(1 for x in cand if x is None) > MAX_MISSING_LENGTHS * len(cand):
        return None
    diffs = [abs(a - b) for a, b in zip(cand, ed) if a is not None and b is not None]
    if not diffs:
        return None
    share = sum(1 for d in diffs if d <= DURATION_TOLERANCE) / len(diffs)
    mean = sum(diffs) / len(diffs)
    kind = "durations" if share == 1.0 else "durations_partial" if share >= 0.8 else "durations_bad"
    return kind, share, mean


# Scoring --------------------------------------------------------------------------------

def score_edition(
    ed: Edition,
    parsed: dict,
    discs: list[list[dict]],
    disc_id_releases: set[str] = frozenset(),
    barcodes_extra: list[str] = (),
) -> EditionScore:
    signals: list[Signal] = []

    def fire(name: str, detail: str) -> None:
        signals.append(Signal(name, WEIGHTS[name], detail))

    if ed.id in disc_id_releases:
        fire("disc_id", "disc ID from the rip log matches this edition")

    ed_cats = {normalize_catalog(c): c for c in ed.catalog_numbers}
    for token in parsed.get("catalog_numbers") or []:
        norm = normalize_catalog(token)
        if norm in ed_cats:
            fire("catalog", f"catalog number {token} = {ed_cats[norm]}")
            break

    ed_barcode = ed.barcode.lstrip("0")
    if ed_barcode:
        for bc in [*(parsed.get("barcodes") or []), *barcodes_extra]:
            if bc.lstrip("0") == ed_barcode:
                fire("barcode", f"barcode {bc} = {ed.barcode}")
                break

    counts = [len(d) for d in discs]
    media = _tracks_agree(counts, ed)
    ed_counts = "+".join(str(n) for n in ed.track_counts) or "?"
    if media is not None:
        fire("tracks", f"{'+'.join(map(str, counts))} tracks = {ed_counts}")
        cand_lengths = [f.get("length") for d in discs for f in d]
        ed_lengths = [x for i in media for x in ed.lengths[i]]
        dur = _durations(cand_lengths, ed_lengths)
        if dur:
            kind, share, mean = dur
            fire(kind, f"{share:.0%} of tracks within ±{DURATION_TOLERANCE:g} s, mean difference {mean:.1f} s")
    elif counts:
        fire("tracks_mismatch", f"{'+'.join(map(str, counts))} tracks ≠ {ed_counts}")

    years = {y for y in (parsed.get("original_year"), parsed.get("reissue_year")) if y}
    if years and ed.year:
        if ed.year in years:
            fire("year", f"year {ed.year} in the path")
        else:
            fire("year_miss", f"path says {', '.join(map(str, sorted(years)))}, edition is {ed.year}")

    label = parsed.get("label")
    if label and ed.labels:
        if label in ed.labels:
            fire("label", f"label {label}")
        else:
            fire("label_miss", f"path says {label}, edition is {', '.join(ed.labels)}")

    source = parsed.get("source")
    families = ed.families
    if source and families:
        if source in families:
            fire("source", f"{source} folder, {ed.format_summary} edition")
        elif not (source == "CD" and "SACD" in families and "hybrid" in " ".join(ed.formats).lower()):
            fire("source_conflict", f"{source} folder, but the edition is {ed.format_summary}")

    remaster = parsed.get("remaster")
    if remaster == "folder" and ed.is_remaster:
        fire("remaster", "remaster in the path and in the edition's name")
    elif remaster == "folder" and not ed.is_remaster:
        fire("remaster_conflict", "remaster in the path, not in the edition's name")
    elif not remaster and ed.is_remaster:
        fire("remaster_unflagged", "the edition is a remaster; the path doesn't say so")

    ed_text = " ".join([ed.title, ed.disambiguation, *ed.formats, *ed.labels, ed.packaging])
    markers = [f for f in parsed.get("flags") or [] if f in MARKER_TEXT and MARKER_TEXT[f].search(ed_text)]
    if markers:
        fire("markers", f"{', '.join(markers)} in the path and the edition")

    territories = set(parsed.get("territories") or [])
    if territories and ed.country:
        if ed.country in territories:
            fire("territory", f"{ed.country} in the path")
        elif not (ed.country == "XE" and territories & EUROPEAN) and ed.country != "XW":
            fire("territory_miss", f"path says {', '.join(sorted(territories))}, edition is {ed.country}")

    names = {s.name for s in signals}
    tier = _tier(names, media is not None)
    return EditionScore(ed.id, tier, round(sum(s.weight for s in signals), 3), signals, media is not None, media or [])


def _tier(names: set[str], tracks_agree: bool) -> str:
    positive = {n for n in names if WEIGHTS[n] > 0}
    if "disc_id" in names:
        return "Exact"
    if not tracks_agree:
        # A track-count mismatch caps the tier at Low.
        return "Low" if positive else "Unknown"
    # Contradictions cap the tier, even with a matching catalog number: labels reuse one
    # number across formats (a WEB folder tagged AKR134 is not the AKR134 cassette when the
    # digital release has no catalog number). Remaster mismatches only lower the score:
    # they are how an original and its remaster are told apart.
    if names & {"source_conflict", "durations_bad"}:
        return "Low"
    if names & {"catalog", "barcode"}:
        return "Exact"
    if "durations" in names and names & {"year", "label", "source"}:
        return "High"
    if positive - {"tracks"}:
        return "Medium"
    return "Low"


def match_candidate(
    files: list[dict],
    parsed: dict,
    editions: list[Edition],
    disc_id_releases: set[str] = frozenset(),
    barcodes_extra: list[str] = (),
) -> MatchResult:
    if not editions:
        return MatchResult()
    discs = candidate_discs(files)
    scores = [score_edition(ed, parsed, discs, disc_id_releases, barcodes_extra) for ed in editions]
    best_rank = max(TIER_RANK[s.tier] for s in scores)
    top = [s for s in scores if TIER_RANK[s.tier] == best_rank]
    best_score = max(s.score for s in top)
    top = [s for s in top if abs(s.score - best_score) < 1e-6]
    if TIERS[best_rank] == "Unknown":
        return MatchResult(tier="Unknown")

    by_id = {e.id: e for e in editions}
    top.sort(key=lambda s: (by_id[s.edition_id].date or "9999", s.edition_id))
    first = top[0]
    first_ed = by_id[first.edition_id]
    return MatchResult(
        tier=TIERS[best_rank],
        editions=[s.edition_id for s in top],
        score=best_score,
        signals=first.signals,
        tracks_agree=first.tracks_agree,
        earliest_date=first_ed.date,
        track_diffs=_track_diffs(discs, first_ed, first.media) if first.tracks_agree else [],
        matched_catalogs=_matched_catalogs(parsed, [by_id[s.edition_id] for s in top]),
    )


def _track_diffs(discs: list[list[dict]], ed: Edition, media: list[int]) -> list[dict]:
    files = [f for d in discs for f in d]
    ed_lengths = [x for i in media for x in ed.lengths[i]]
    out = []
    for f, ed_len in zip(files, ed_lengths):
        cand_len = f.get("length")
        diff = cand_len - ed_len if cand_len is not None and ed_len is not None else None
        out.append({"name": split_path(f["filename"])[-1], "length": cand_len, "edition_length": ed_len, "diff": diff})
    return out


def _matched_catalogs(parsed: dict, eds: list[Edition]) -> list[str]:
    cats = {normalize_catalog(c) for e in eds for c in e.catalog_numbers}
    return [t for t in parsed.get("catalog_numbers") or [] if normalize_catalog(t) in cats]


SECURE_RIPPERS = {"EAC", "XLD", "dBpoweramp", "CUERipper", "whipper"}


def rip_quality(sidecars: dict, parsed: dict, log_info: dict | None = None) -> dict:
    """Separate score out of 4: log present, secure ripper, AccurateRip or checksum results, cue present.

    Before the log is read, the ripper comes from the path and the results are unknown.
    """
    log, cue = bool(sidecars.get("log")), bool(sidecars.get("cue"))
    logs = (log_info or {}).get("logs") or []
    if logs:
        secure = all(l.get("secure") for l in logs)
        verified = any(l.get("accurate") or (l.get("crc_checked") and not l.get("crc_mismatches")) or l.get("checksum")
                       for l in logs) and not any(l.get("verdict") == "bad" for l in logs)
    else:
        secure = log and parsed.get("rip_tool") in SECURE_RIPPERS
        verified = False
    return {
        "log": log, "cue": cue, "secure_ripper": bool(secure), "verified": bool(verified), "read": bool(logs),
        "score": int(log) + int(bool(secure)) + int(bool(verified)) + int(cue),
    }


def completeness(match: MatchResult | dict, sidecars: dict) -> dict:
    tracks = match["tracks_agree"] if isinstance(match, dict) else match.tracks_agree
    art = bool(sidecars.get("image"))
    return {"tracks": tracks, "art": art, "score": int(tracks) + int(art)}
