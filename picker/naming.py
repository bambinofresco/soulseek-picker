"""Album folder names, built the way Lidarr builds them.

Lidarr has no separate album folder format: the album folder is the directory part of
its track format (e.g. `{Album Title} ({Release Year})/{track:00} - {Track Title}`).
The picker's `album_folder_template` should equal that directory part, so picker albums
and Lidarr albums sit side by side with identical names. This module follows Lidarr's
FileNameBuilder (develop branch, October 2026):

- Token syntax `{prefix Token Name suffix}`: when a token renders empty, its prefix and
  suffix inside the braces go too, so `{ [Catalog Number]}` vanishes without a catalog
  number. Literal text outside braces is kept as written.
- Each token value is cleaned like Lidarr's CleanFileName with Replace Illegal Characters
  on and the default "Smart" colon replacement: `: ` -> ` - `, `:` -> `-`, `\\ /` -> `+`,
  `?` -> `!`, `*` -> `-`, `< > | "` removed; leading spaces/dots and trailing spaces trimmed.
- The finished folder name is trimmed, runs of the same separator (`- . _` or space) are
  collapsed to one, trailing separators are stripped, and Windows reserved names
  (`con.`, `aux.`, ...) get their dot replaced.
- `{Release Year}` is the album's release year, or `Unknown` when there is none, so a
  missing year gives `Album (Unknown)` exactly like Lidarr.

Lidarr's own tokens supported here: Album Title, Release Year, Artist Name, Album Type,
Album Disambiguation, Album MbId, Artist MbId. Picker extras from the chosen edition (not
in Lidarr): Catalog Number, Label, Country, Media, Edition Date, Edition Year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN_RE = re.compile(
    r"\{(?P<prefix>[- ._\[(]*)(?P<token>[a-z0-9]+(?:(?P<sep>[- ._]+)[a-z0-9]+)*)(?P<suffix>[- ._)\]]*)\}",
    re.IGNORECASE,
)
BAD_CHARS = {"\\": "+", "/": "+", "<": "", ">": "", "?": "!", "*": "-", "|": "", '"': ""}
REPEATED_SEPARATOR = re.compile(r"([- ._])(\1)+")
TRAILING_SEPARATORS = re.compile(r"[- ._]+$")
RESERVED = re.compile(r"^(aux|com[1-9]|con|lpt[1-9]|nul|prn)\.", re.IGNORECASE)

# Older picker templates used {album} / {year} / {artist}.
ALIASES = {"album": "album title", "year": "release year", "artist": "artist name"}

TOKENS = [
    "Album Title", "Release Year", "Artist Name", "Album Type", "Album Disambiguation", "Album MbId", "Artist MbId",
    "Catalog Number", "Label", "Country", "Media", "Edition Date", "Edition Year",
]


@dataclass
class FolderValues:
    album_title: str
    release_year: str | None = None  # None renders "Unknown", like Lidarr
    artist_name: str = ""
    album_type: str = ""
    album_disambiguation: str = ""
    album_mbid: str = ""
    artist_mbid: str = ""
    catalog_number: str = ""
    label: str = ""
    country: str = ""
    media: str = ""
    edition_date: str = ""

    def lookup(self) -> dict[str, str]:
        return {
            "album title": self.album_title,
            "release year": self.release_year or "Unknown",
            "artist name": self.artist_name,
            "album type": self.album_type,
            "album disambiguation": self.album_disambiguation,
            "album mbid": self.album_mbid,
            "artist mbid": self.artist_mbid,
            "catalog number": self.catalog_number,
            "label": self.label,
            "country": self.country,
            "media": self.media,
            "edition date": self.edition_date,
            "edition year": self.edition_date[:4],
        }


def _key(token: str) -> str:
    key = re.sub(r"[- ._]+", " ", token).strip().lower()
    return ALIASES.get(key, key)


def unknown_tokens(template: str) -> list[str]:
    known = {t.lower() for t in TOKENS} | set(ALIASES)
    return [m.group("token") for m in TOKEN_RE.finditer(template) if _key(m.group("token")) not in known]


def clean_value(value: str) -> str:
    """Lidarr's CleanFileName with Replace Illegal Characters on and Smart colon replacement."""
    value = (value or "").strip().replace(": ", " - ").replace(":", "-")
    value = "".join(BAD_CHARS.get(ch, ch) for ch in value)
    return value.lstrip(" .").rstrip(" ")


def clean_component(name: str) -> str:
    name = REPEATED_SEPARATOR.sub(r"\1", name.strip())
    name = TRAILING_SEPARATORS.sub("", name)
    return RESERVED.sub(lambda m: m.group(1) + "_", name)


def album_folder(template: str, values: FolderValues) -> str:
    bad = unknown_tokens(template)
    if bad:
        raise ValueError(f"unknown token(s) in the album folder template: {', '.join(bad)}")
    lookup = values.lookup()

    def render(m: re.Match) -> str:
        value = clean_value(lookup[_key(m.group("token"))])
        if not value:
            return ""
        sep = m.group("sep")
        if sep and sep != " ":
            value = value.replace(" ", sep)
        return f"{m.group('prefix')}{value}{m.group('suffix')}"

    return clean_component(TOKEN_RE.sub(render, template)) or "Unknown"
