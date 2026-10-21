"""Group slskd search responses into candidates: one user's album folder each.

Pure functions over the JSON slskd returns, so they can be tested without a server.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

AUDIO_EXTS = {"flac", "mp3", "m4a", "aac", "alac", "ogg", "opus", "wav", "aif", "aiff", "ape", "wv", "dsf", "dff", "wma"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff"}
TEXT_EXTS = {"txt", "nfo", "md5", "sfv", "ffp", "accurip"}
PLAYLIST_EXTS = {"m3u", "m3u8"}

# "CD1", "Disc 2", "Disc 2 - Live", "Disk03", "CD 1 of 2", "Side A"
DISC_DIR_RE = re.compile(r"^(?:cd|dis[ck])\s*[-_.]?\s*(\d{1,2})\b|^side\s*[-_.]?\s*([a-h])\b", re.I)
# Folders that hold artwork for the album folder above them.
SCAN_DIR_RE = re.compile(r"^(?:scans?|artwork|art|covers?|booklet|images?|pictures?)$", re.I)


def split_path(remote: str) -> list[str]:
    return [seg for seg in re.split(r"[\\/]", remote) if seg]


def extension(filename: str) -> str:
    name = split_path(filename)[-1] if filename else ""
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def file_kind(filename: str) -> str:
    ext = extension(filename)
    if ext in AUDIO_EXTS:
        return "audio"
    if ext == "log":
        return "log"
    if ext == "cue":
        return "cue"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TEXT_EXTS:
        return "text"
    if ext in PLAYLIST_EXTS:
        return "playlist"
    return "other"


def disc_number(segment: str) -> int | None:
    m = DISC_DIR_RE.match(segment.strip())
    if not m:
        return None
    if m.group(1):
        return int(m.group(1))
    return ord(m.group(2).lower()) - ord("a") + 1


@dataclass
class Placement:
    folder: list[str]  # the candidate folder's segments
    disc: str | None = None  # the disc subfolder name, if any
    in_scans: bool = False


def place(filename: str, kind: str) -> Placement:
    """Work out which candidate folder a file belongs to."""
    segs = split_path(filename)[:-1]
    in_scans = False
    if kind != "audio":
        # Scans/, Artwork/Front/, CD1/Scans/ ... attach to the album folder above.
        for i in range(max(len(segs) - 3, 1), len(segs)):
            if SCAN_DIR_RE.match(segs[i]):
                segs = segs[:i]
                in_scans = True
                break
    disc = None
    if len(segs) > 1 and disc_number(segs[-1]) is not None:
        disc = segs[-1]
        segs = segs[:-1]
    return Placement(segs, disc, in_scans)


@dataclass
class CandidateDraft:
    username: str
    folder: str
    query: str
    files: dict[str, dict] = field(default_factory=dict)  # keyed by full remote filename
    has_free_slot: bool = False
    queue_length: int = 0
    upload_speed: int = 0

    def summary(self) -> dict:
        """Columns for the Candidate row."""
        files = sorted(self.files.values(), key=lambda f: f["filename"].lower())
        audio = [f for f in files if f["kind"] == "audio"]

        per_disc: Counter = Counter()
        for f in audio:
            per_disc[f["disc"]] += 1
        # Loose files first, then discs in numeric order.
        disc_keys = sorted(per_disc, key=lambda d: (d is not None, disc_number(d) if d else 0, d or ""))
        disc_counts = [per_disc[d] for d in disc_keys]

        exts = Counter(extension(f["filename"]) for f in audio)
        if len(exts) == 1:
            audio_format = next(iter(exts)).upper()
        else:
            audio_format = "mixed" if exts else ""

        depths = Counter(f["bitDepth"] for f in audio if f.get("bitDepth"))
        rates = Counter(f["sampleRate"] for f in audio if f.get("sampleRate"))

        sidecars = Counter(f["kind"] for f in files if f["kind"] != "audio")
        sidecars["scans"] = sum(1 for f in files if f.get("in_scans"))

        return {
            "files": files,
            "audio_count": len(audio),
            "disc_counts": disc_counts,
            "total_size": sum(f.get("size") or 0 for f in files),
            "audio_format": audio_format,
            "bit_depth": depths.most_common(1)[0][0] if depths else None,
            "sample_rate": rates.most_common(1)[0][0] if rates else None,
            "mixed_quality": len(depths) > 1 or len(rates) > 1,
            "sidecars": {k: v for k, v in sidecars.items() if v},
        }


def group_responses(responses: list[dict], query: str, drafts: dict[tuple[str, str], CandidateDraft] | None = None) -> dict[tuple[str, str], CandidateDraft]:
    """Fold one search's responses into drafts keyed by (username, folder).

    Pass the drafts from an earlier query to merge a fallback query's results in.
    """
    drafts = {} if drafts is None else drafts
    for resp in responses:
        username = resp.get("username")
        if not username:
            continue
        for f in resp.get("files", []) or []:
            filename = f.get("filename") or ""
            kind = file_kind(filename)
            placement = place(filename, kind)
            if not placement.folder:
                continue
            folder = "\\".join(placement.folder)
            key = (username, folder)
            draft = drafts.get(key)
            if draft is None:
                draft = drafts[key] = CandidateDraft(username=username, folder=folder, query=query)
            draft.has_free_slot = bool(resp.get("hasFreeUploadSlot"))
            draft.queue_length = int(resp.get("queueLength") or 0)
            draft.upload_speed = int(resp.get("uploadSpeed") or 0)
            draft.files.setdefault(filename, {
                "filename": filename,
                "name": split_path(filename)[-1],
                "size": int(f.get("size") or 0),
                "kind": kind,
                "disc": placement.disc,
                "in_scans": placement.in_scans,
                "bitDepth": f.get("bitDepth"),
                "sampleRate": f.get("sampleRate"),
                "bitRate": f.get("bitRate"),
                "length": f.get("length"),
            })
    return drafts


def candidate_drafts(drafts: dict[tuple[str, str], CandidateDraft]) -> list[CandidateDraft]:
    """Drop folders with no audio (stray artwork folders and the like)."""
    return [d for d in drafts.values() if any(f["kind"] == "audio" for f in d.files.values())]


def passes_default_filter(audio_format: str, audio_count: int, min_track_count: int | None, flac_only: bool) -> bool:
    """FLAC only, at least 3 audio files, and at least half the smallest edition's track count."""
    if flac_only and audio_format != "FLAC":
        return False
    if audio_count < 3:
        return False
    if min_track_count and audio_count * 2 < min_track_count:
        return False
    return True


def availability_key(has_free_slot: bool, queue_length: int, upload_speed: int) -> tuple:
    """Sort key: free slot first, then shortest queue, then fastest upload."""
    return (not has_free_slot, queue_length, -upload_speed)
