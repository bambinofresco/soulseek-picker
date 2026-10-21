"""Turn a candidate's remote paths into structured hints about which edition it is.

`parse_candidate` is a pure function. Every value it reports carries the Evidence it
came from (token, path segment, meaning) so the UI can show why it guessed what it did.

Directory segments from the share root down to the album folder (and disc subfolders)
are the main input. Before scanning, the artist name and album title are blanked out of
each segment, so an album called "Deluxe" or "1999", or an artist called "Japan", does not
read as an edition marker. File names are only used where the spec says so: track-level
remaster mentions (weak) and rip-tool names in sidecar files.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from difflib import SequenceMatcher

from .grouping import file_kind, place, split_path
from .queries import fold

# Pattern tables -------------------------------------------------------------------------

I = re.IGNORECASE

# Order matters: the first source matched in the deepest segment wins.
SOURCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SACD", re.compile(r"(?<![A-Za-z])SA-?CD(?![A-Za-z])", I)),
    ("DVD-A", re.compile(r"(?<![A-Za-z])DVD[- ]?A(?:udio)?(?![A-Za-z])", I)),
    ("Blu-ray", re.compile(r"(?<![A-Za-z])(?:Blu-?ray|BD-?A(?:udio)?|Pure Audio)(?![A-Za-z])", I)),
    ("Vinyl", re.compile(r"(?<![A-Za-z])(?:vinyl(?:[- ]?rip)?|needle-?\s?drop|LP-?rip)(?![A-Za-z])", I)),
    ("Vinyl", re.compile(r"(?<![A-Za-z])\d?LP(?![A-Za-z0-9])(?!\s*\d)")),
    ("Cassette", re.compile(r"(?<![A-Za-z])(?:cassette|tape[- ]?rip)(?![A-Za-z])", I)),
    ("WEB", re.compile(r"(?<![A-Za-z])WEB(?:-?DL|-?FLAC|-?rip)?(?![A-Za-z])")),
    ("CD", re.compile(r"(?<![A-Za-z])(?:CD-?rip|CDDA|HDCD|SHM-?CD|U?HQCD|Blu-?spec)(?![A-Za-z])", I)),
    # "CD", "2CD", "SHM-CD"; not "CD1" / "CD 2" (disc folders) or the CD in "TPLP71CD".
    ("CD", re.compile(r"(?<![A-Za-z0-9])\d?CD(?![A-Za-z0-9])(?!\s*\d{1,2}(?!\d))", I)),
]
# Bracket contents that alone name a source: "(Web)", "[web]".
BRACKET_SOURCE = {"web": "WEB", "cd": "CD", "vinyl": "Vinyl", "lp": "Vinyl"}

STORES: list[tuple[str, re.Pattern]] = [
    (name, re.compile(rf"(?<![A-Za-z]){pattern}(?![A-Za-z])", I))
    for name, pattern in [
        ("Qobuz", r"qobuz"),
        ("Bandcamp", r"bandcamp"),
        ("HDtracks", r"hd-?tracks"),
        ("Tidal", r"tidal"),
        ("iTunes", r"itunes"),
        ("Apple Music", r"apple music"),
        ("Amazon", r"amazon"),
        ("Deezer", r"deezer"),
        ("7digital", r"7digital"),
        ("Beatport", r"beatport"),
        ("Bleep", r"bleep"),
        ("ProStudioMasters", r"pro ?studio ?masters"),
        ("Presto", r"presto ?music"),
    ]
]

REMASTER_RE = re.compile(r"(?<![A-Za-z])re-?master(?:ed|ing|s)?(?![A-Za-z])", I)
RM_RE = re.compile(r"(?<![A-Za-z])RM(?![A-Za-z])")
REISSUE_RE = re.compile(r"(?<![A-Za-z])re-?issue[ds]?(?![A-Za-z])", I)

FLAG_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Deluxe", re.compile(r"(?<![A-Za-z])deluxe(?![A-Za-z])", I)),
    ("Expanded", re.compile(r"(?<![A-Za-z])expanded(?![A-Za-z])", I)),
    ("Anniversary", re.compile(r"(?<![A-Za-z])(?:\d{1,3}(?:st|nd|rd|th)\s+)?anniversary(?![A-Za-z])", I)),
    ("Bonus", re.compile(r"(?<![A-Za-z])bonus(?![A-Za-z])", I)),
    ("Reissue", REISSUE_RE),
    ("SHM-CD", re.compile(r"(?<![A-Za-z])SHM(?:[- ]?CD)?(?![A-Za-z])", I)),
    ("Blu-spec", re.compile(r"(?<![A-Za-z])blu-?spec(?:\s?CD\s?2?)?(?![A-Za-z])", I)),
    ("MFSL", re.compile(r"(?<![A-Za-z])(?:MFSL|MoFi|UDCD|mobile fidelity(?: sound lab)?)(?![A-Za-z])", I)),
    ("DCC", re.compile(r"(?<![A-Za-z])DCC(?![A-Za-z])|compact classics", 0)),
    ("Gold", re.compile(r"(?<![A-Za-z])(?:24\s?k(?:t|arat)?\s+gold|gold\s+(?:cd|disc|edition))(?![A-Za-z])", I)),
    ("First press", re.compile(r"(?<![A-Za-z])(?:1st|first)[ -]?press(?:ing)?(?![A-Za-z])", I)),
    ("Promo", re.compile(r"(?<![A-Za-z])promo(?:tional)?(?![A-Za-z])", I)),
    ("OOP", re.compile(r"(?<![A-Za-z])OOP(?![A-Za-z])")),
    ("Mono", re.compile(r"(?<![A-Za-z])mono(?![A-Za-z])", I)),
    ("Stereo", re.compile(r"(?<![A-Za-z])stereo(?![A-Za-z])", I)),
]
# Bracket contents that alone are a flag: "[Gold]".
BRACKET_FLAG = {"gold": "Gold"}

# Territory words -> MusicBrainz country codes. Short codes are upper-case only ("us" is a word).
TERRITORY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("JP", re.compile(r"(?<![A-Za-z])japan(?:ese)?(?![A-Za-z])", I)),
    ("JP", re.compile(r"(?<![A-Za-z])JPN?(?![A-Za-z])")),
    ("GB", re.compile(r"(?<![A-Za-z])UK(?![A-Za-z])")),
    ("GB", re.compile(r"(?<![A-Za-z])united kingdom(?![A-Za-z])", I)),
    ("US", re.compile(r"(?<![A-Za-z])USA?(?![A-Za-z])")),
    ("DE", re.compile(r"(?<![A-Za-z])german(?:y)?(?![A-Za-z])", I)),
    ("XE", re.compile(r"(?<![A-Za-z])EU(?![A-Za-z])")),
    ("XE", re.compile(r"(?<![A-Za-z])europe(?:an)?(?![A-Za-z])", I)),
    ("FR", re.compile(r"(?<![A-Za-z])france(?![A-Za-z])", I)),
    ("CA", re.compile(r"(?<![A-Za-z])canad(?:a|ian)(?![A-Za-z])", I)),
    ("AU", re.compile(r"(?<![A-Za-z])australia(?:n)?(?![A-Za-z])", I)),
    ("NL", re.compile(r"(?<![A-Za-z])(?:netherlands|holland)(?![A-Za-z])", I)),
    ("KR", re.compile(r"(?<![A-Za-z])korea(?:n)?(?![A-Za-z])", I)),
    ("TW", re.compile(r"(?<![A-Za-z])taiwan(?:ese)?(?![A-Za-z])", I)),
    ("IT", re.compile(r"(?<![A-Za-z])ital(?:y|ian)(?![A-Za-z])", I)),
    ("BR", re.compile(r"(?<![A-Za-z])brazil(?:ian)?(?![A-Za-z])", I)),
    ("SE", re.compile(r"(?<![A-Za-z])swed(?:en|ish)(?![A-Za-z])", I)),
    ("AT", re.compile(r"(?<![A-Za-z])austria(?:n)?(?![A-Za-z])", I)),
    ("ES", re.compile(r"(?<![A-Za-z])(?:spain|spanish)(?![A-Za-z])", I)),
    ("MX", re.compile(r"(?<![A-Za-z])mexic(?:o|an)(?![A-Za-z])", I)),
    ("AR", re.compile(r"(?<![A-Za-z])argentin(?:a|ian)(?![A-Za-z])", I)),
    ("PL", re.compile(r"(?<![A-Za-z])pol(?:and|ish)(?![A-Za-z])", I)),
    ("CZ", re.compile(r"(?<![A-Za-z])czech(?:oslovakia)?(?![A-Za-z])", I)),
    ("GR", re.compile(r"(?<![A-Za-z])gree(?:ce|k)(?![A-Za-z])", I)),
    ("DK", re.compile(r"(?<![A-Za-z])(?:denmark|danish)(?![A-Za-z])", I)),
    ("NO", re.compile(r"(?<![A-Za-z])norw(?:ay|egian)(?![A-Za-z])", I)),
    ("FI", re.compile(r"(?<![A-Za-z])fin(?:land|nish)(?![A-Za-z])", I)),
    ("BE", re.compile(r"(?<![A-Za-z])belgi(?:um|an)(?![A-Za-z])", I)),
    ("NZ", re.compile(r"(?<![A-Za-z])new zealand(?![A-Za-z])", I)),
    ("HK", re.compile(r"(?<![A-Za-z])hong kong(?![A-Za-z])", I)),
    ("RU", re.compile(r"(?<![A-Za-z])russia(?:n)?(?![A-Za-z])", I)),
]

RIP_TOOL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EAC", re.compile(r"(?<![A-Za-z])(?:EAC|exact audio copy)(?![A-Za-z])", I)),
    ("XLD", re.compile(r"(?<![A-Za-z])(?:XLD|x lossless decoder)(?![A-Za-z])", I)),
    ("dBpoweramp", re.compile(r"(?<![A-Za-z])(?:dbpoweramp|dBpa)(?![A-Za-z])", I)),
    ("CUERipper", re.compile(r"(?<![A-Za-z])cue ?ripper(?![A-Za-z])", I)),
    ("whipper", re.compile(r"(?<![A-Za-z])whipper(?![A-Za-z])", I)),
]

YEAR_RE = re.compile(r"(?<![A-Za-z0-9])((?:19|20)\d{2})(?![0-9])")
# Upper-case words directly before a number that are not catalog prefixes.
NOT_CATALOG_PREFIX = {
    "CD", "CDS", "CDR", "LP", "EP", "WEB", "SACD", "HDCD", "DSD", "RM", "UK", "US", "USA", "EU", "JP", "JPN",
    "OOP", "FLAC", "MP", "AAC", "ALAC", "OGG", "DISC", "DISK", "SIDE", "VOL", "PART", "PT", "TRACK", "TR",
    "BONUS", "LIVE", "DEMO", "TOUR", "BOX", "SET", "EDITION", "EAC", "XLD", "KHZ", "BIT", "BITS", "HD",
    "DVD", "BD", "VBR", "CBR", "KBPS", "V", "MIX", "RIP", "PCM", "WAV",
}
CATALOG_ALPHA_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{1,7})([ .-]?)(\d{1,7})((?:[-.]\d{1,5}){0,3}(?:[~/]\d{1,2})?[A-Z]{0,3})(?![A-Za-z0-9])"
)
# EMI-style trailing digit groups: "CDP 7243 8 55229 2 5".
CATALOG_GROUPS_RE = re.compile(r"(?: \d{1,5}){1,4}(?![A-Za-z0-9])")
CATALOG_NUMERIC_RE = re.compile(r"(?<![\d])(\d{3,5}(?:[ -]\d{1,7}){2,5})(?![\d])")
DATE_RE = re.compile(r"^(?:19|20)\d{2}[-.](?:0[1-9]|1[0-2])[-.](?:0[1-9]|[12]\d|3[01])$")
BARCODE_RE = re.compile(r"(?<!\d)(\d{12,13})(?!\d)")
BRACKET_RE = re.compile(r"[\[({]([^\[\](){}]*)[\])}]")

RATE = r"44[.,]1|44|48|88[.,]2|88|96|176[.,]4|176|192|352[.,]8|384"
DEPTH_RATE_RE = re.compile(rf"(?<![\d.])(16|24|32)\s*(?:-?bits?|b)?\s*[-/]\s*({RATE})(?:\s*-?k(?:hz)?)?(?![\d])", I)
DEPTH_RE = re.compile(r"(?<![\d.])(16|24|32)\s*-?\s*bits?(?![A-Za-z])", I)
RATE_RE = re.compile(rf"(?<![\d.])({RATE})\s*-?\s*khz(?![A-Za-z])", I)
RATE_HZ = {"44": 44100, "44.1": 44100, "48": 48000, "88": 88200, "88.2": 88200, "96": 96000,
           "176": 176400, "176.4": 176400, "192": 192000, "352.8": 352800, "384": 384000}

LABEL_SUFFIXES = {"records", "recordings", "record", "music", "entertainment", "ltd", "inc", "label",
                  "company", "co", "group", "gmbh", "llc", "corp", "corporation", "limited", "productions"}


# Result types ---------------------------------------------------------------------------

@dataclass
class Evidence:
    token: str
    segment: str
    meaning: str


@dataclass
class ParsedFolder:
    original_year: int | None = None
    reissue_year: int | None = None
    years: list[int] = field(default_factory=list)
    catalog_numbers: list[str] = field(default_factory=list)  # candidates, not facts
    barcodes: list[str] = field(default_factory=list)
    label: str | None = None
    source: str | None = None  # CD | WEB | Vinyl | SACD | DVD-A | Blu-ray | Cassette
    store: str | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None  # Hz
    mixed_quality: bool = False
    quality_from: str | None = None  # "files" (slskd attributes) | "path"
    remaster: str | None = None  # "folder" (strong) | "track" (weak)
    flags: list[str] = field(default_factory=list)
    territories: list[str] = field(default_factory=list)
    rip_tool: str | None = None
    sidecars: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, list[Evidence]] = field(default_factory=dict)

    def note(self, key: str, token: str, segment: str, meaning: str) -> None:
        items = self.evidence.setdefault(key, [])
        if not any(e.token == token and e.segment == segment and e.meaning == meaning for e in items):
            items.append(Evidence(token.strip(), segment, meaning))

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_catalog(text: str) -> str:
    """Upper case with spaces, hyphens and dots removed: 'cdp 7243-8' -> 'CDP72438'."""
    return re.sub(r"[\s.\-]", "", text).upper()


# Helpers ---------------------------------------------------------------------------------

def _phrase_re(phrase: str) -> re.Pattern | None:
    words = re.findall(r"[a-z0-9]+", fold(phrase or "").lower())
    if not words:
        return None
    body = r"[\W_]*".join(map(re.escape, words))
    if words[0] == "the" and len(words) > 1:
        body = r"(?:the[\W_]*)?" + r"[\W_]*".join(map(re.escape, words[1:]))
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", I)


def _bracket_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in BRACKET_RE.finditer(text)]


def _blank(text: str, pattern: re.Pattern | None) -> str:
    """Replace the first match outside brackets with spaces, keeping positions.

    "Japan - Tin Drum [Japan]": the artist is blanked, the territory tag is kept.
    """
    if pattern is None:
        return text
    spans = _bracket_spans(text)
    for m in pattern.finditer(text):
        if not any(s <= m.start() < e for s, e in spans):
            return text[: m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    return text


@dataclass
class _Segment:
    raw: str  # as it appears in the path
    text: str  # folded, with artist and album title blanked out
    brackets: list[str]  # contents of each (), [] or {} group in text


def _segments(paths: Sequence[str], artist: str, album: str) -> list[_Segment]:
    """Directory segments from root to album folder, then disc folders; scans folders excluded."""
    chains = []
    discs: list[str] = []
    for p in paths:
        pl = place(p, file_kind(p))
        chains.append(pl.folder)
        if pl.disc and pl.disc not in discs:
            discs.append(pl.disc)
    if not chains:
        return []
    prefix = list(chains[0])
    for chain in chains[1:]:
        n = 0
        while n < min(len(prefix), len(chain)) and prefix[n] == chain[n]:
            n += 1
        prefix = prefix[:n]

    artist_re, album_re = _phrase_re(artist), _phrase_re(album)
    out = []
    for raw in prefix + discs:
        text = fold(raw)
        text = _blank(text, album_re)
        text = _blank(text, artist_re)
        out.append(_Segment(raw, text, [b.strip() for b in BRACKET_RE.findall(text)]))
    return out


def _label_words(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", fold(name).lower())
    while len(words) > 1 and words[-1] in LABEL_SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return words


def _find_label(text: str, labels: Iterable[str]) -> tuple[str, str] | None:
    """(label name, matched text) for the edition label found in a segment.

    When several match ("EMI" and "Toshiba-EMI"), the longest wins.
    """
    hits = [hit for label in labels if (hit := _match_label(text, label))]
    return max(hits, key=lambda hit: len(hit[1])) if hits else None


def _match_label(text: str, label: str) -> tuple[str, str] | None:
    """Exact word-sequence match, or a close fuzzy one for names of 6+ characters."""
    if not label or label.strip().lower() in ("[no label]", "no label"):
        return None
    words = _label_words(label)
    if not words or len("".join(words)) < 2:
        return None
    seg_words = re.findall(r"[a-z0-9]+", text.lower())
    target = " ".join(words)
    for i in range(len(seg_words) - len(words) + 1):
        joined = " ".join(seg_words[i:i + len(words)])
        if joined == target:
            return label, joined
        if len(target) >= 6 and SequenceMatcher(None, joined, target).ratio() >= 0.88:
            return label, joined
    return None


def _year_is_catalog_or_noise(text: str, start: int, end: int) -> bool:
    before, after = text[:start], text[end:]
    # An upper-case catalog prefix joined to it: "ABC-1987", "ABC1987". With a space
    # ("OKNOTOK 1997") it is read as a year.
    prefix = re.search(r"(?<![A-Za-z0-9])([A-Z]{1,7})[-.]?$", before)
    if prefix and prefix.group(1) not in NOT_CATALOG_PREFIX:
        return True
    # Inside a longer number ("24-1987", "1987-2"), but a range "1997-2017" or a date is fine.
    if re.search(r"\d[-./]$", before) and not re.search(r"(?:19|20)\d{2}\s*-\s*$", before):
        return True
    m = re.match(r"[-./](\d+)", after)
    return bool(m and not re.fullmatch(r"\d{2}|(?:19|20)\d{2}", m.group(1)))


# The parser ------------------------------------------------------------------------------

def parse_candidate(
    paths: Sequence[str],
    *,
    files: Sequence[dict] | None = None,
    artist: str = "",
    album: str = "",
    labels: Sequence[str] = (),
    current_year: int | None = None,
) -> ParsedFolder:
    """Parse one candidate.

    paths: every remote file path in the candidate folder (backslash or slash separated).
    files: optional slskd file entries ({"filename", "bitDepth", "sampleRate", ...}).
    artist, album: blanked out of folder names before scanning.
    labels: label names of this album's editions; the only labels that can match.
    """
    current_year = current_year or date.today().year
    out = ParsedFolder()
    segments = _segments(paths, artist, album)
    deepest_first = list(reversed(segments))

    _parse_years(out, segments, current_year)
    _parse_catalog(out, segments)
    _parse_source(out, deepest_first)
    _parse_label(out, deepest_first, labels)
    _parse_quality(out, segments, paths, files)
    _parse_remaster(out, segments, paths, album)
    _parse_markers(out, segments)
    _parse_rip_tool(out, segments, paths)
    _parse_sidecars(out, paths)
    return out


def _parse_years(out: ParsedFolder, segments: list[_Segment], current_year: int) -> None:
    found: dict[int, bool] = {}  # year -> sits next to a remaster/reissue word
    for seg in segments:
        for m in YEAR_RE.finditer(seg.text):
            year = int(m.group(1))
            if year > current_year + 1 or _year_is_catalog_or_noise(seg.text, m.start(1), m.end(1)):
                continue
            # "Near" a remaster word: in the same bracket group, else within a few characters.
            span = next(((s, e) for s, e in _bracket_spans(seg.text) if s <= m.start() < e), None)
            window = seg.text[span[0]:span[1]] if span else seg.text[max(0, m.start() - 14): m.end() + 14]
            near =bool(REMASTER_RE.search(window) or RM_RE.search(window) or REISSUE_RE.search(window))
            found[year] = found.get(year, False) or near
            out.note("years", m.group(1), seg.raw, "remaster/reissue year" if near else "year")
    years = sorted(found)
    out.years = years
    if len(years) >= 2:
        out.original_year, out.reissue_year = years[0], years[-1]
    elif years:
        if found[years[0]]:
            out.reissue_year = years[0]
        else:
            out.original_year = years[0]


def _parse_catalog(out: ParsedFolder, segments: list[_Segment]) -> None:
    seen: set[str] = set()

    def add(token: str, seg: _Segment, meaning: str) -> None:
        key = normalize_catalog(token)
        if any(key in s for s in seen):  # "7243 8 55229 2 5" inside "CDP 7243 8 55229 2 5"
            return
        seen.add(key)
        out.catalog_numbers.append(token)
        out.note("catalog_numbers", token, seg.raw, meaning)

    for seg in segments:
        for m in CATALOG_ALPHA_RE.finditer(seg.text):
            prefix, sep, digits, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
            if prefix in NOT_CATALOG_PREFIX:
                continue
            if sep == " " and not suffix and YEAR_RE.fullmatch(digits):
                continue  # "OKNOTOK 1997" is a year (see _year_is_catalog_or_noise)
            if sep == " " and len(prefix) == 1:
                continue  # "A 24-96" in "DVD-A 24-96"
            token = m.group(0)
            groups = CATALOG_GROUPS_RE.match(seg.text, m.end())
            if groups and not suffix and not YEAR_RE.search(groups.group(0)):
                token += groups.group(0)
            n_digits = len(re.sub(r"\D", "", token))
            if n_digits < 3 and not (n_digits == 2 and len(prefix) >= 3):
                continue
            add(token, seg, "catalog number candidate")
        for content in seg.brackets:
            for m in CATALOG_NUMERIC_RE.finditer(content):
                token = m.group(1)
                digits = re.sub(r"\D", "", token)
                if DATE_RE.match(token) or not 8 <= len(digits) <= 14:
                    continue
                add(token, seg, "catalog number candidate (numeric)")
                if len(digits) in (12, 13) and digits not in out.barcodes:
                    out.barcodes.append(digits)
                    out.note("barcodes", token, seg.raw, "barcode (12/13 digits)")
        for m in BARCODE_RE.finditer(seg.text):
            if m.group(1) not in out.barcodes:
                out.barcodes.append(m.group(1))
                out.note("barcodes", m.group(1), seg.raw, "barcode (12/13 digits)")


def _parse_source(out: ParsedFolder, deepest_first: list[_Segment]) -> None:
    for seg in deepest_first:
        for name, pattern in STORES:
            m = pattern.search(seg.text)
            if m:
                out.note("source", m.group(0), seg.raw, f"store: {name} (WEB)")
                if out.store is None:
                    out.store = name
                if out.source is None:
                    out.source = "WEB"
        for name, pattern in SOURCE_PATTERNS:
            m = pattern.search(seg.text)
            if m:
                out.note("source", m.group(0), seg.raw, f"source: {name}")
                if out.source is None:
                    out.source = name
        for content in seg.brackets:
            name = BRACKET_SOURCE.get(content.lower())
            if name:
                out.note("source", content, seg.raw, f"source: {name}")
                if out.source is None:
                    out.source = name


def _parse_label(out: ParsedFolder, deepest_first: list[_Segment], labels: Sequence[str]) -> None:
    for seg in deepest_first:
        hit = _find_label(seg.text, labels)
        if hit:
            out.label = hit[0]
            out.note("label", hit[1], seg.raw, f"label: {hit[0]}")
            return


def _parse_quality(out: ParsedFolder, segments: list[_Segment], paths: Sequence[str], files: Sequence[dict] | None) -> None:
    audio = [f for f in (files or []) if file_kind(f.get("filename", "")) == "audio"]
    depths = Counter(f["bitDepth"] for f in audio if f.get("bitDepth"))
    rates = Counter(f["sampleRate"] for f in audio if f.get("sampleRate"))
    if depths or rates:
        out.quality_from = "files"
        out.bit_depth = depths.most_common(1)[0][0] if depths else None
        out.sample_rate = rates.most_common(1)[0][0] if rates else None
        out.mixed_quality = len(depths) > 1 or len(rates) > 1
        detail = ", ".join(f"{d}-bit ×{n}" for d, n in depths.most_common()) or "no bit depth"
        detail += "; " + (", ".join(f"{r / 1000:g} kHz ×{n}" for r, n in rates.most_common()) or "no sample rate")
        out.note("quality", "slskd file attributes", "", detail)
        return

    for seg in reversed(segments):
        m = DEPTH_RATE_RE.search(seg.text)
        if m:
            out.bit_depth = int(m.group(1))
            out.sample_rate = RATE_HZ[m.group(2).replace(",", ".")]
            out.note("quality", m.group(0), seg.raw, "bit depth / sample rate from path")
            break
        d, r = DEPTH_RE.search(seg.text), RATE_RE.search(seg.text)
        if d or r:
            if d:
                out.bit_depth = int(d.group(1))
                out.note("quality", d.group(0), seg.raw, "bit depth from path")
            if r:
                out.sample_rate = RATE_HZ[r.group(1).replace(",", ".")]
                out.note("quality", r.group(0), seg.raw, "sample rate from path")
            break
    if out.bit_depth or out.sample_rate:
        out.quality_from = "path"


def _parse_remaster(out: ParsedFolder, segments: list[_Segment], paths: Sequence[str], album: str) -> None:
    for seg in segments:
        m = REMASTER_RE.search(seg.text) or RM_RE.search(seg.text)
        if m:
            out.remaster = "folder"
            out.note("remaster", m.group(0), seg.raw, "remaster word in folder name (strong)")
    if out.remaster:
        return
    album_re = _phrase_re(album)
    for p in paths:
        if file_kind(p) != "audio":
            continue
        name = split_path(p)[-1]
        m = REMASTER_RE.search(_blank(fold(name), album_re))
        if m:
            out.remaster = "track"
            out.note("remaster", m.group(0), name, "remaster word in a track name (weak)")
            break


def _parse_markers(out: ParsedFolder, segments: list[_Segment]) -> None:
    for seg in segments:
        for name, pattern in FLAG_PATTERNS:
            m = pattern.search(seg.text)
            if m:
                if name not in out.flags:
                    out.flags.append(name)
                out.note("flags", m.group(0), seg.raw, name)
        for content in seg.brackets:
            name = BRACKET_FLAG.get(content.lower())
            if name:
                if name not in out.flags:
                    out.flags.append(name)
                out.note("flags", content, seg.raw, name)
        for code, pattern in TERRITORY_PATTERNS:
            m = pattern.search(seg.text)
            if m:
                if code not in out.territories:
                    out.territories.append(code)
                out.note("territories", m.group(0), seg.raw, f"territory: {code}")


def _parse_rip_tool(out: ParsedFolder, segments: list[_Segment], paths: Sequence[str]) -> None:
    texts = [(seg.text, seg.raw) for seg in segments]
    texts += [(fold(split_path(p)[-1]), split_path(p)[-1]) for p in paths if file_kind(p) != "audio"]
    for text, raw in texts:
        for name, pattern in RIP_TOOL_PATTERNS:
            m = pattern.search(text)
            if m:
                out.note("rip_tool", m.group(0), raw, f"rip tool: {name} (confirmed once the log is read)")
                if out.rip_tool is None:
                    out.rip_tool = name


def _parse_sidecars(out: ParsedFolder, paths: Sequence[str]) -> None:
    counts: Counter = Counter()
    for p in paths:
        kind = file_kind(p)
        if kind in ("log", "cue", "image", "text"):
            counts[kind] += 1
        if kind != "audio" and place(p, kind).in_scans:
            counts["scans"] += 1
    out.sidecars = dict(counts)
