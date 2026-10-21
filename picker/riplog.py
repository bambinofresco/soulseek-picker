"""Read CD rip logs and cue sheets: the TOC, the MusicBrainz disc ID, and rip quality.

All pure functions over bytes, so they can be tested against fixture logs.

Disc ID calculation (https://musicbrainz.org/doc/Disc_ID_Calculation, libdiscid disc.c):
track offsets are start sectors + 150 (the two-second pregap); the lead-out is the last
audio track's end sector + 1 + 150. Neither EAC nor XLD labels data tracks, so a gap of
about 11,400 sectors before the last track marks an Enhanced CD data track: it is dropped
and the lead-out becomes its start + 150 - 11,400.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import asdict, dataclass, field

PREGAP = 150
DATA_TRACK_GAP = 11400
DATA_TRACK_TOLERANCE = 300

TOC_HEADER = re.compile(r"TOC of the extracted CD", re.I)
TOC_ROW = re.compile(r"^\s*(\d{1,2})\s*\|\s*[\d:.]+\s*\|\s*[\d:.]+\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$")


def decode(data: bytes) -> str:
    """EAC writes UTF-16LE with a BOM; XLD writes UTF-8. Fall back to UTF-8, then Latin-1."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    sample = data[:400]
    if len(sample) >= 4 and sample.count(b"\x00") >= len(sample) // 4:  # UTF-16 without a BOM
        odd_nulls = sample[1::2].count(b"\x00")
        return data.decode("utf-16-le" if odd_nulls >= sample[0::2].count(b"\x00") else "utf-16-be", "replace")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


@dataclass
class TocEntry:
    track: int
    start: int
    end: int


@dataclass
class DiscId:
    id: str
    first: int
    last: int
    leadout: int
    offsets: list[int]
    data_track_dropped: bool = False

    @property
    def toc_param(self) -> str:
        """The `toc` query parameter MusicBrainz takes: first last leadout offsets..."""
        return " ".join(str(x) for x in [self.first, self.last, self.leadout, *self.offsets])


def parse_tocs(text: str) -> list[list[TocEntry]]:
    """Every 'TOC of the extracted CD' block (a multi-disc log has several)."""
    tocs: list[list[TocEntry]] = []
    current: list[TocEntry] | None = None
    for line in text.splitlines():
        if TOC_HEADER.search(line):
            current = []
            tocs.append(current)
            continue
        if current is None:
            continue
        m = TOC_ROW.match(line)
        if m:
            current.append(TocEntry(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        elif current and line.strip() and not line.strip().startswith("-"):
            current = None  # first non-row line after the rows ends the block
    return [t for t in tocs if t]


def disc_id(toc: list[TocEntry]) -> DiscId:
    entries = sorted(toc, key=lambda e: e.track)
    dropped = False
    leadout = entries[-1].end + 1 + PREGAP
    if len(entries) > 1:
        gap = entries[-1].start - entries[-2].end - 1
        if abs(gap - DATA_TRACK_GAP) <= DATA_TRACK_TOLERANCE:
            leadout = entries[-1].start + PREGAP - DATA_TRACK_GAP
            entries = entries[:-1]
            dropped = True
    first, last = entries[0].track, entries[-1].track
    offsets = [e.start + PREGAP for e in entries]
    slots = [leadout] + [0] * 99
    for e, off in zip(entries, offsets):
        slots[e.track] = off
    raw = f"{first:02X}{last:02X}" + "".join(f"{x:08X}" for x in slots)
    digest = base64.b64encode(hashlib.sha1(raw.encode("ascii")).digest()).decode("ascii")
    return DiscId(
        id=digest.translate(str.maketrans("+/=", "._-")),
        first=first, last=last, leadout=leadout, offsets=offsets, data_track_dropped=dropped,
    )


# Everything else the log says ------------------------------------------------------------

@dataclass
class LogInfo:
    ripper: str | None = None  # EAC | XLD | other name | None
    parsed: bool = False  # TOC read (EAC and XLD only)
    read_mode: str | None = None
    secure: bool | None = None
    disc_ids: list[DiscId] = field(default_factory=list)
    tracks: int = 0
    accurate: int = 0  # tracks AccurateRip confirmed
    inaccurate: int = 0  # tracks AccurateRip says may not be accurate
    crc_mismatches: int = 0
    crc_checked: int = 0
    errors: bool | None = None  # final status line; None if absent
    checksum: bool = False  # the ripper's log checksum / signature is present

    @property
    def verdict(self) -> str:
        """good | bad | unknown, for the log icon."""
        if not self.parsed:
            return "unknown"
        if self.errors or self.crc_mismatches or self.inaccurate or self.secure is False:
            return "bad"
        if self.errors is False and (self.accurate or self.crc_checked):
            return "good"
        return "unknown"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict
        for disc, obj in zip(d["disc_ids"], self.disc_ids):
            disc["toc"] = obj.toc_param
        return d


def identify_ripper(text: str) -> str | None:
    head = text[:2000]
    if "Exact Audio Copy" in head or "EAC extraction logfile" in head:
        return "EAC"
    if "X Lossless Decoder" in head or "XLD extraction logfile" in head:
        return "XLD"
    for name in ("CUERipper", "dBpoweramp", "whipper", "morituri", "Rubyripper", "cdparanoia"):
        if name.lower() in head.lower():
            return name
    return None


def parse_log(data: bytes) -> LogInfo:
    text = decode(data)
    info = LogInfo(ripper=identify_ripper(text))
    if info.ripper not in ("EAC", "XLD"):
        return info  # recorded as "log present, not parsed"

    tocs = parse_tocs(text)
    info.parsed = bool(tocs)
    info.disc_ids = [disc_id(t) for t in tocs]
    info.tracks = sum(len(t) for t in tocs)

    if info.ripper == "EAC":
        m = re.search(r"Read mode\s*:\s*(\S+)", text)
        info.read_mode = m.group(1) if m else None
        info.secure = (info.read_mode or "").lower() == "secure" if m else None
        tests = re.findall(r"Test CRC\s+([0-9A-F]{8})", text, re.I)
        copies = re.findall(r"Copy CRC\s+([0-9A-F]{8})", text, re.I)
        info.checksum = "==== Log checksum" in text
    else:
        m = re.search(r"Ripper mode\s*:\s*(.+)", text)
        info.read_mode = m.group(1).strip() if m else None
        info.secure = ("secure" in (info.read_mode or "").lower() or "paranoia" in (info.read_mode or "").lower()) if m else None
        tests = re.findall(r"CRC32 hash \(test run\)\s*:\s*([0-9A-F]{8})", text, re.I)
        copies = re.findall(r"CRC32 hash\s*:\s*([0-9A-F]{8})", text, re.I)
        info.checksum = "-----BEGIN XLD SIGNATURE-----" in text

    pairs = list(zip(tests, copies))
    info.crc_checked = len(pairs)
    info.crc_mismatches = sum(1 for t, c in pairs if t.upper() != c.upper())
    info.accurate = len(re.findall(r"Accurately ripped", text, re.I)) - len(re.findall(r"All tracks accurately ripped", text, re.I))
    info.inaccurate = len(re.findall(r"Rip may not be accurate|Accurately ripped.{0,40}\bNG\b", text, re.I))

    if re.search(r"No errors occurr?ed", text, re.I):
        info.errors = False
    elif re.search(r"There were errors|Some inconsistencies found|errors occurr?ed", text, re.I):
        info.errors = True
    return info


@dataclass
class CueInfo:
    catalog: str | None = None  # UPC/EAN
    isrcs: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"catalog": self.catalog, "isrcs": {str(k): v for k, v in self.isrcs.items()}}


def parse_cue(data: bytes) -> CueInfo:
    text = decode(data)
    info = CueInfo()
    m = re.search(r'^\s*CATALOG\s+"?(\d{12,13})"?\s*$', text, re.M)
    if m and set(m.group(1)) != {"0"}:
        info.catalog = m.group(1)
    track = None
    for line in text.splitlines():
        t = re.match(r"\s*TRACK\s+(\d+)\s+AUDIO", line, re.I)
        if t:
            track = int(t.group(1))
            continue
        i = re.match(r'\s*ISRC\s+"?([A-Z]{2}[A-Z0-9]{3}\d{7})"?', line, re.I)
        if i and track is not None:
            info.isrcs[track] = i.group(1).upper()
    return info
