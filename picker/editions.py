"""Editions: the releases in a release group, reduced to what matching and the UI need."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

VIDEO_FORMATS = {"dvd-video", "dvd", "blu-ray", "vhs", "hd-dvd", "videodisc"}
REMASTER_RE = re.compile(r"re-?master", re.I)


def format_family(fmt: str | None) -> str | None:
    """MusicBrainz medium format -> the parser's source names."""
    f = (fmt or "").lower()
    if not f:
        return None
    if "vinyl" in f:
        return "Vinyl"
    if "cassette" in f:
        return "Cassette"
    if "digital" in f:
        return "WEB"
    if "sacd" in f:
        return "SACD"
    if "dvd-audio" in f:
        return "DVD-A"
    if "blu-ray" in f:
        return "Blu-ray"
    if "dvd" in f:
        return "DVD"
    if "cd" in f or "blu-spec" in f:
        return "CD"
    return None


@dataclass
class Edition:
    id: str
    title: str
    disambiguation: str = ""
    date: str = ""
    country: str = ""
    status: str = ""
    packaging: str = ""
    barcode: str = ""
    labels: list[str] = field(default_factory=list)
    catalog_numbers: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)  # per medium
    track_counts: list[int] = field(default_factory=list)  # per medium
    lengths: list[list[float | None]] = field(default_factory=list)  # seconds, per medium
    disc_ids: list[list[str]] = field(default_factory=list)  # per medium

    @property
    def year(self) -> int | None:
        return int(self.date[:4]) if self.date[:4].isdigit() else None

    @property
    def audio_media(self) -> list[int]:
        return [i for i, f in enumerate(self.formats) if (f or "").lower() not in VIDEO_FORMATS]

    @property
    def families(self) -> set[str]:
        return {fam for i in self.audio_media if (fam := format_family(self.formats[i]))}

    @property
    def is_remaster(self) -> bool:
        return bool(REMASTER_RE.search(f"{self.title} {self.disambiguation}"))

    @property
    def format_summary(self) -> str:
        """'CD', '2×CD', 'CD + DVD-Video', '12" Vinyl'."""
        parts: list[tuple[str, int]] = []
        for fmt in self.formats:
            fmt = fmt or "?"
            if parts and parts[-1][0] == fmt:
                parts[-1] = (fmt, parts[-1][1] + 1)
            else:
                parts.append((fmt, 1))
        return " + ".join(f"{n}×{fmt}" if n > 1 else fmt for fmt, n in parts)

    def to_dict(self) -> dict:
        return asdict(self)


def edition_from_release(release: dict) -> Edition:
    media = sorted(release.get("media", []) or [], key=lambda m: m.get("position") or 0)
    lengths = []
    for medium in media:
        row = []
        for track in medium.get("tracks", []) or []:
            ms = track.get("length") or (track.get("recording") or {}).get("length")
            row.append(ms / 1000 if ms else None)
        lengths.append(row)
    label_info = release.get("label-info", []) or []
    return Edition(
        id=release["id"],
        title=release.get("title", ""),
        disambiguation=release.get("disambiguation", "") or "",
        date=release.get("date", "") or "",
        country=release.get("country", "") or "",
        status=release.get("status", "") or "",
        packaging=release.get("packaging", "") or "",
        barcode=release.get("barcode", "") or "",
        labels=[n for li in label_info if (n := (li.get("label") or {}).get("name"))],
        catalog_numbers=[c for li in label_info if (c := li.get("catalog-number")) and c.lower() != "[none]"],
        formats=[m.get("format") or "" for m in media],
        track_counts=[len(m.get("tracks") or []) or m.get("track-count", 0) for m in media],
        lengths=lengths,
        disc_ids=[[d["id"] for d in m.get("discs", []) or [] if d.get("id")] for m in media],
    )


def editions_from_releases(releases: list[dict]) -> list[Edition]:
    """Editions sorted by date (undated last), then country."""
    eds = [edition_from_release(r) for r in releases]
    return sorted(eds, key=lambda e: (e.date or "9999", e.country, e.id))
