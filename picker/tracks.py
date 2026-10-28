"""Tracks-only downloads (spec 7.1): which of an uploader's files are the tracks wanted.

The chosen edition's track list gives each wanted recording a medium and a position. The
folder's files are matched to positions by duration first (the response carries each file's
length, and the matcher pairs whole folders the same way), with the track number parsed from
the file name as the tie-breaker, since many uploads have no numbers or disc-relative ones.
A folder in which any wanted track cannot be identified is not eligible.
"""

from __future__ import annotations

import re

from .editions import Edition
from .grouping import split_path
from .matching import candidate_discs
from .models import Candidate

DURATION_TOLERANCE = 3.0  # seconds; a little looser than the matcher's 2 s, one file at a time
TRACK_NUMBER = re.compile(r"^\s*(?:\d{1,2}\s*[-.]\s*)?(\d{1,3})\s*[-._ )]")  # "07 Title", "1-07 Title", "07. Title"


def file_track_number(filename: str) -> int | None:
    name = split_path(filename)[-1]
    m = TRACK_NUMBER.match(name)
    return int(m.group(1)) if m else None


def _disc_for_medium(discs: list[list[dict]], ed: Edition, medium: int) -> tuple[list[dict], int]:
    """The folder's files holding this medium, and the offset of the medium's first track in them."""
    audio_media = ed.audio_media or list(range(len(ed.track_counts)))
    if len(discs) == len(audio_media):
        return discs[audio_media.index(medium)] if medium in audio_media else [], 0
    if len(discs) == 1:
        # One folder holding every disc: positions run on from one medium to the next.
        offset = sum(ed.track_counts[i] for i in audio_media if i < medium)
        return discs[0], offset
    return [], 0


def _pick(files: list[dict], ed_length: float | None, number: int) -> dict | None:
    by_duration = [f for f in files if ed_length is not None and f.get("length") is not None
                   and abs(float(f["length"]) - ed_length) <= DURATION_TOLERANCE]
    by_number = [f for f in files if file_track_number(f["filename"]) == number]
    both = [f for f in by_duration if f in by_number]
    if len(both) == 1:
        return both[0]
    if len(by_duration) == 1:
        return by_duration[0]
    if ed_length is None and len(by_number) == 1:
        return by_number[0]
    if len(both) > 1:
        return both[0]
    if len(by_number) == 1 and not by_duration:
        return by_number[0]
    return None


def wanted_files(candidate: Candidate, edition: Edition, recordings: list[str]) -> list[str] | None:
    """Remote file names of the wanted recordings in this folder, or None when one cannot be found."""
    discs = candidate_discs(candidate.files or [])
    if not discs or not recordings:
        return None
    chosen: list[str] = []
    for recording in recordings:
        position = edition.position_of(recording)
        if position is None:
            return None  # the edition does not carry this recording
        medium, index = position
        files, offset = _disc_for_medium(discs, edition, medium)
        if not files:
            return None
        ed_length = edition.lengths[medium][index] if index < len(edition.lengths[medium]) else None
        number = offset + index + 1 if len(discs) == 1 and offset else index + 1
        pool = [f for f in files if f["filename"] not in chosen]
        found = _pick(pool, ed_length, number)
        if found is None:
            return None  # cannot be told apart: this folder is not eligible for a tracks-only download
        chosen.append(found["filename"])
    return chosen
