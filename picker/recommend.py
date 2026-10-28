"""The recommendation card: which folder to download, and why not the others.

Eligible folders are those at the best match tier present (Exact if any Exact exists).
Ties are broken, in order, by verification (disc ID or a verified log, then catalog number
or barcode), the edition preferences, completeness and sidecars, the estimated time to
obtain the folder (queue wait plus size over upload speed), and finally size.

The estimate uses the response's upload speed and queue length, or the speed measured on a
download from that user in this session; an unknown speed counts as slow and a free slot
as no queue wait. The same estimate is shown in the table's Availability column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from .config import Settings
from .db import session
from .editions import Edition
from .matching import TIER_RANK, completeness, rip_quality
from .models import Candidate, Download
from .preferences import PrefScore, best_edition, score_candidate

SLOW_BPS = 300_000  # an uploader with no speed reported: assume a slow one
QUEUED_FILE_BYTES = 40_000_000  # a typical FLAC track, for queue waits (slskd reports files, not bytes)
MEASURE_MIN_BYTES = 20_000_000
MEASURE_WINDOW = timedelta(hours=24)


@dataclass
class Estimate:
    seconds: float | None  # None: nothing to go on
    queue_seconds: float
    speed: int  # bytes/s used
    measured: bool
    detail: str  # "free slot, 2.4 MB/s"

    @property
    def short(self) -> str:
        """'~3 min', '~40 min (queue)', 'unknown'."""
        if self.seconds is None:
            return "unknown"
        text = _minutes(self.seconds)
        return f"~{text} (queue)" if self.queue_seconds > self.seconds / 2 else f"~{text}"


def _minutes(seconds: float) -> str:
    minutes = max(1, int(seconds / 60 + 0.5))  # 150 s is 'about 3 min', not banker's 2
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _speed_text(bps: int) -> str:
    return f"{bps / 1_000_000:.1f} MB/s" if bps >= 1_000_000 else f"{bps / 1000:.0f} kB/s"


def measured_speeds(usernames: set[str]) -> dict[str, int]:
    """Bytes/s seen on this session's downloads from these users (the fastest recent one)."""
    if not usernames:
        return {}
    since = datetime.now(timezone.utc) - MEASURE_WINDOW
    with session() as s:
        rows = s.exec(select(Download).where(Download.username.in_(usernames), Download.created_at >= since)).all()
    speeds: dict[str, int] = {}
    for d in rows:
        if d.state not in ("transferring", "completed") or d.bytes_done < MEASURE_MIN_BYTES:
            continue
        end = d.finished_at or datetime.now(timezone.utc)
        start = d.created_at if d.created_at.tzinfo else d.created_at.replace(tzinfo=timezone.utc)
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        elapsed = (end - start).total_seconds()
        if elapsed > 0:
            speeds[d.username] = max(speeds.get(d.username, 0), int(d.bytes_done / elapsed))
    return speeds


def estimate(c: Candidate, measured: dict[str, int] | None = None) -> Estimate:
    measured = measured or {}
    speed = measured.get(c.username) or c.upload_speed or 0
    known = speed > 0
    speed = speed if known else SLOW_BPS
    queue = 0.0 if c.has_free_slot else (c.queue_length or 0) * QUEUED_FILE_BYTES / speed
    transfer = (c.total_size or 0) / speed
    parts = ["free slot" if c.has_free_slot else f"queue of {c.queue_length}"]
    if c.username in measured:
        parts.append(f"{_speed_text(speed)} measured")
    elif known:
        parts.append(_speed_text(speed))
    else:
        parts.append("speed unknown")
    seconds = queue + transfer if (known or c.has_free_slot or c.queue_length) else None
    if not known and not c.has_free_slot and not c.queue_length:
        seconds = None
    return Estimate(seconds, queue, speed, c.username in measured, ", ".join(parts))


def verification(c: Candidate) -> tuple[int, str]:
    """3: disc ID or a verified log; 2: catalog number or barcode; 0: the rest."""
    m = c.match or {}
    names = {s.get("name") for s in m.get("signals") or []}
    if "disc_id" in names:
        return 3, "disc ID"
    rq = rip_quality(c.sidecars or {}, c.parsed or {}, c.log_info)
    if rq["verified"]:
        return 3, "verified log"
    if "catalog" in names:
        return 2, "catalog number"
    if "barcode" in names:
        return 2, "barcode"
    return 0, ""


@dataclass
class Pick:
    candidate: Candidate
    tier: str
    verification: str
    pref: PrefScore
    edition: Edition | None  # the edition to preselect
    complete: dict
    rip: dict
    eta: Estimate
    summary: list[str] = field(default_factory=list)  # the card's one-line facts


@dataclass
class Recommendation:
    best: Pick
    runner_up: Pick | None
    why_not: list[tuple[Candidate, str]]  # others at the same tier or below, with the short reason


def _pick(c: Candidate, editions: dict[str, Edition], settings: Settings, measured: dict[str, int]) -> Pick:
    m = c.match or {}
    matched = [editions[r] for r in m.get("editions") or [] if r in editions]
    pref = score_candidate(c.parsed or {}, c.bit_depth or (c.parsed or {}).get("bit_depth"), matched, settings)
    rank, how = verification(c)
    comp = completeness(m if "tracks_agree" in m else {"tracks_agree": False}, c.sidecars or {})
    rip = rip_quality(c.sidecars or {}, c.parsed or {}, c.log_info)
    eta = estimate(c, measured)
    summary = [f"{m.get('tier') or 'Unknown'}" + (f" by {how}" if how else "")]
    if len(matched) > 1:
        summary.append(f"one of {len(matched)} editions")
    if rip["log"]:
        logs = (c.log_info or {}).get("logs") or []
        acc = sum(l.get("accurate") or 0 for l in logs)
        summary.append(("verified log" if rip["verified"] else "log") + (f", AccurateRip {acc} ok" if acc else ""))
    summary.append("complete" if comp["tracks"] else "track count differs")
    if comp["art"]:
        summary.append("cover art")
    depth = c.bit_depth or (c.parsed or {}).get("bit_depth")
    rate = c.sample_rate or (c.parsed or {}).get("sample_rate")
    if depth or rate:
        summary.append(f"{depth or '?'}/{(rate or 0) / 1000:g}" if rate else f"{depth}-bit")
    summary.append(eta.detail + (f" → about {_minutes(eta.seconds)}" if eta.seconds is not None else ""))
    return Pick(c, m.get("tier") or "Unknown", how, pref, best_edition(matched, settings), comp, rip, eta, summary)


def _key(p: Pick) -> tuple:
    return (
        -(3 if p.verification in ("disc ID", "verified log") else 2 if p.verification else 0),
        -p.pref.score,
        -(p.complete["score"] + p.rip["score"]),
        p.eta.seconds if p.eta.seconds is not None else 10**9,
        p.candidate.total_size or 0,
    )


def _why_not(other: Pick, best: Pick) -> str:
    reasons = []
    if TIER_RANK.get(other.tier, 0) < TIER_RANK.get(best.tier, 0):
        reasons.append(f"{other.tier} match")
    elif best.verification and not other.verification:
        reasons.append("no " + best.verification.replace("verified log", "verified log").replace("disc ID", "disc ID"))
    m = other.candidate.match or {}
    if len(m.get("editions") or []) > 1:
        reasons.append(f"one of {len(m['editions'])} editions")
    if other.pref.score < best.pref.score:
        reasons.append(", ".join(r for r in other.pref.reasons if r.startswith("−")) or f"preferences {other.pref.label}")
    if (other.candidate.audio_format or "").upper() != "FLAC":
        reasons.append(other.candidate.audio_format or "not FLAC")
    if not other.complete["tracks"] and best.complete["tracks"]:
        reasons.append(f"{other.candidate.audio_count} tracks")
    if best.rip["log"] and not other.rip["log"]:
        reasons.append("no log")
    if other.eta.seconds is not None and best.eta.seconds is not None and other.eta.seconds > best.eta.seconds * 2:
        reasons.append(f"slower ({other.eta.short})")
    elif not other.candidate.has_free_slot and other.candidate.queue_length:
        reasons.append(f"queue of {other.candidate.queue_length}")
    if not reasons:
        reasons.append("same on every count; bigger" if (other.candidate.total_size or 0) > (best.candidate.total_size or 0) else "same on every count")
    return "; ".join(reasons)


def recommend(candidates: list[Candidate], editions: dict[str, Edition], settings: Settings) -> Recommendation | None:
    """None when there is nothing matched well enough to recommend (no tier above Unknown)."""
    rated = [c for c in candidates if (c.match or {}).get("tier") in TIER_RANK and (c.match or {}).get("tier") != "Unknown"]
    if not rated:
        return None
    measured = measured_speeds({c.username for c in rated})
    picks = sorted((_pick(c, editions, settings, measured) for c in rated), key=lambda p: (-TIER_RANK[p.tier], *_key(p)))
    best = picks[0]
    same_tier = [p for p in picks[1:] if p.tier == best.tier]
    runner_up = same_tier[0] if same_tier else (picks[1] if len(picks) > 1 else None)
    why_not = [(p.candidate, _why_not(p, best)) for p in picks[1:6]]
    return Recommendation(best, runner_up, why_not)
