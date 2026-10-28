"""Compare the tags the uploader left in the files with the edition chosen at download.

beets no longer counts album-level tags against the pinned match (see beets.py), so a
folder tagged as the 2011 remaster would be retagged as the 1997 original without a word.
This check reads those tags before anything is placed or tagged and sorts them into:

    same       the files already carry the chosen release's MusicBrainz ID
    sibling    they carry another release of this album with the same year and the same
               kind of edition: a sister pressing (country, label, catalog number differ)
    different  they name another edition (remaster, reissue, another year) or a release
               outside this album, or say "remaster" when the chosen edition isn't one
    mixed      the files name more than one release between them: a folder assembled from
               several rips
    agree      no MusicBrainz ID, but the year or remaster words that are there fit
    untagged   nothing to compare
    unreadable the tags could not be read

`different` and `mixed` hold an import for review; the Review page offers the tagged
edition with one click, and Import tags the files as chosen regardless.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import mutagen

from .editions import REMASTER_RE, Edition

TAG_KEYS = ["musicbrainz_albumid", "musicbrainz_releasegroupid", "album", "date", "originaldate", "comment", "version"]
# Words in a release's disambiguation that mark a different edition rather than a sister pressing.
EDITION_WORDS = re.compile(r"re-?master|re-?issue|deluxe|anniversary|expanded|special|collector|limited|bonus|"
                           r"edition|box set|mono|stereo|remix", re.I)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass
class TagCheck:
    kind: str
    detail: str
    tagged_mbid: str | None = None  # the release the files name, when they name one
    in_group: bool = False  # ... and it is an edition of this album
    tags: dict | None = None  # what was read: tag -> value (the most common across the files)
    split: list[dict] | None = None  # mixed: [{"mbid", "count"}, ...], most files first

    def to_dict(self) -> dict:
        return asdict(self)


def _easy_tags(path: Path) -> dict:
    info = mutagen.File(path, easy=True)
    raw = getattr(info, "tags", None) or {}
    found = {}
    for key in TAG_KEYS:
        try:
            values = raw.get(key)
        except (KeyError, ValueError):
            values = None
        if values:
            value = values[0] if isinstance(values, list) else values
            found[key] = str(value).strip()
    return found


def read_tags(files: list[Path]) -> tuple[dict, list[str], Counter]:
    """The most common value of each tag across the files, the files that could not be read,
    and how many files carry each release ID."""
    counts: dict[str, Counter] = {key: Counter() for key in TAG_KEYS}
    errors = []
    for path in files:
        try:
            tags = _easy_tags(path)
        except Exception as exc:  # noqa: BLE001 - mutagen raises many types on odd files
            errors.append(f"{path.name}: {exc}")
            continue
        for key, value in tags.items():
            counts[key][value] += 1
    return {key: c.most_common(1)[0][0] for key, c in counts.items() if c}, errors, counts["musicbrainz_albumid"]


def _year(text: str | None) -> int | None:
    return int(text[:4]) if text and text[:4].isdigit() else None


def _edition_words(ed: Edition) -> set[str]:
    return {w.lower().replace("-", "") for w in EDITION_WORDS.findall(f"{ed.title} {ed.disambiguation}")}


def compare(tags: dict, chosen: Edition | None, editions: list[Edition]) -> TagCheck:
    """Sort the files' tags against the chosen edition (see the module docstring)."""
    tags = {k: v for k, v in tags.items() if v}
    if chosen is None:
        return TagCheck("untagged", "no edition chosen to compare with", tags=tags)
    mbid = (tags.get("musicbrainz_albumid") or "").lower()
    tagged = next((e for e in editions if e.id.lower() == mbid), None) if mbid else None
    if mbid and (tagged or UUID_RE.match(mbid)):  # anything else is not a release ID
        if mbid == chosen.id.lower():
            return TagCheck("same", "the files already carry this edition's MusicBrainz ID", mbid, True, tags)
        if tagged is None:
            return TagCheck("different", "the files are tagged as a release that is not an edition of this album",
                            mbid, False, tags)
        reasons = []
        if tagged.year and chosen.year and tagged.year != chosen.year:
            reasons.append(f"dated {tagged.year}, not {chosen.year}")
        if tagged.is_remaster != chosen.is_remaster:
            reasons.append("a remaster" if tagged.is_remaster else "not a remaster")
        elif _edition_words(tagged) != _edition_words(chosen):
            reasons.append(f"'{tagged.disambiguation or tagged.title}', not '{chosen.disambiguation or chosen.title}'")
        if reasons:
            return TagCheck("different", "the files are tagged as another edition of this album: " + ", ".join(reasons),
                            mbid, True, tags)
        return TagCheck("sibling", "the files are tagged as a sister pressing of this edition (same year and kind; "
                                   "country, label or catalog number differ)", mbid, True, tags)
    # No usable MusicBrainz ID: fall back to what the words and dates say.
    words = " ".join(tags.get(k, "") for k in ("album", "comment", "version"))
    if REMASTER_RE.search(words) and not chosen.is_remaster:
        return TagCheck("different", "the files' tags say remaster; the chosen edition is not one", tags=tags)
    year = _year(tags.get("date"))
    if year and chosen.year and year != chosen.year:
        return TagCheck("different", f"the files are dated {year}; the chosen edition is from {chosen.year}", tags=tags)
    if year or REMASTER_RE.search(words):
        return TagCheck("agree", "no MusicBrainz release in the tags; the year fits the chosen edition", tags=tags)
    return TagCheck("untagged", "the files carry no release ID, date or edition words to compare", tags=tags)


def check_folder(files: list[Path], chosen: Edition | None, editions: list[Edition]) -> TagCheck:
    tags, errors, ids = read_tags(files)
    if not tags and errors:
        return TagCheck("unreadable", "could not read the tags: " + errors[0], tags={})
    releases = [(mbid, n) for mbid, n in ids.most_common() if UUID_RE.match(mbid)]
    if len(releases) > 1:
        split = [{"mbid": mbid.lower(), "count": n} for mbid, n in releases]
        return TagCheck("mixed", f"the files name {len(releases)} different releases between them "
                                 f"({', '.join(f'{n} tagged {mbid[:8]}' for mbid, n in releases)}): "
                                 "a folder put together from several rips", tags=tags, split=split)
    return compare(tags, chosen, editions)
