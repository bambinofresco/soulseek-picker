"""Rip verification against CUETools DB and AccurateRip.

`flac -t` proves a file decodes, not that it holds the right audio. These two databases
store checksums of real CD rips, keyed by the disc's table of contents (TOC). The folder's
tracks are decoded to CD audio, their checksums computed the way those databases expect,
and compared with what other people's rips produced.

Formulas were taken from the whipper and CUETools sources (both agree) and checked against
live lookups; see README "Rip verification". In short:

    TOC          LBA of each track's start (track 1 = 0) and the leadout, from the EAC/XLD
                 log in the folder, or built from the track lengths rounded up to sectors
    AccurateRip  per-track v1 = sum(sample * index) over the track (track 1 skips its
                 first 2939 samples, the last track its last 2940); v2 adds the high words
                 of the 64-bit products; a "frame 450" sum detects the pressing offset
    CTDB         CRC32 of the disc's audio minus 5880 samples at the start and
                 5880 + 588 * (sectors mod 10) at the end, plus one CRC32 per track

Only 16-bit 44.1 kHz stereo folders qualify (CD audio); anything else is "not applicable".
Lookups are cached for a day. Verification is a signal: no match is common for obscure
pressings. Only a track that fails while its siblings verify holds an import for review.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import struct
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx
import mutagen
import numpy as np

from . import __version__
from .config import Settings
from .db import cache_get, cache_put
from .riplog import TocEntry

SECTOR = 588  # stereo samples per CD sector
PREGAP = 150
DATA_TRACK_GAP = 11400  # sectors between the last audio track and a CD-Extra data track
DATA_TRACK_TOLERANCE = 2
AR_SKIP_FIRST = 5 * SECTOR - 1  # 2939
AR_SKIP_LAST = 5 * SECTOR  # 2940
AR_OFFSET_RANGE = 5 * SECTOR - 1  # pressing offsets searched: -2939 .. +2939
CTDB_STRIDE = 10 * SECTOR * 2  # 11760 16-bit units
CTDB_PREFIX = CTDB_STRIDE // 2  # 5880 samples
LOOKUP_TTL = timedelta(hours=24)
AR_BASE = "http://www.accuraterip.com/accuraterip"
CTDB_URL = "http://db.cuetools.net/lookup2.php"


# TOC ---------------------------------------------------------------------------------------

@dataclass
class Toc:
    offsets: list[int]  # LBA of each track's INDEX 01, every track including a data track
    audio: list[bool]
    leadout: int  # LBA one past the end of the last track (a trailing data track included)
    source: str  # "log" | "lengths"

    @property
    def audio_offsets(self) -> list[int]:
        return [o for o, a in zip(self.offsets, self.audio) if a]

    @property
    def audio_leadout(self) -> int:
        """Where the audio ends: before the gap to a data track, else the leadout."""
        if self.audio and not self.audio[-1]:
            return self.offsets[-1] - DATA_TRACK_GAP
        return self.leadout

    @property
    def audio_sectors(self) -> list[int]:
        offs = self.audio_offsets + [self.audio_leadout]
        return [b - a for a, b in zip(offs, offs[1:])]

    def ctdb_string(self) -> str:
        return ":".join(("" if a else "-") + str(o) for o, a in zip(self.offsets, self.audio)) + ":" + str(self.leadout)


def toc_from_log(entries: list[TocEntry]) -> Toc:
    """From an EAC/XLD log's TOC rows (start/end sectors are LBAs). A last track sitting
    11400 sectors after the previous one is a CD-Extra data track."""
    rows = sorted(entries, key=lambda e: e.track)
    audio = [True] * len(rows)
    if len(rows) > 1 and abs(rows[-1].start - rows[-2].end - 1 - DATA_TRACK_GAP) <= DATA_TRACK_TOLERANCE:
        audio[-1] = False
    return Toc([r.start for r in rows], audio, rows[-1].end + 1, "log")


def toc_from_lengths(sample_counts: list[int]) -> Toc | None:
    """The way CUETools builds a TOC from bare track files: lengths rounded up to whole
    sectors, track 1 at LBA 0 (the standard pregap is implicit)."""
    if not sample_counts or any(n <= 0 for n in sample_counts):
        return None
    offsets, lba = [], 0
    for n in sample_counts:
        if n % SECTOR == 492:
            n -= 4608  # some rippers append 4608 samples; CUETools strips them
        offsets.append(lba)
        lba += (n + SECTOR - 1) // SECTOR
    return Toc(offsets, [True] * len(offsets), lba, "lengths")


# AccurateRip ---------------------------------------------------------------------------------

def ar_ids(toc: Toc) -> tuple[int, int, int]:
    id1 = id2 = 0
    n = 0
    for off, audio in zip(toc.offsets, toc.audio):
        if not audio:
            continue
        n += 1
        id1 += off
        id2 += max(off, 1) * n
    id1 += toc.leadout
    id2 += max(toc.leadout, 1) * (n + 1)

    def digsum(x: int) -> int:
        return sum(int(ch) for ch in str(x))

    total = sum(digsum(off // 75 + 2) for off in toc.offsets)
    length = toc.leadout // 75 - toc.offsets[0] // 75
    cddb = ((total % 255) << 24 | (length & 0xFFFF) << 8 | len(toc.offsets)) & 0xFFFFFFFF
    return id1 & 0xFFFFFFFF, id2 & 0xFFFFFFFF, cddb


def ar_path(toc: Toc) -> str:
    id1, id2, cddb = ar_ids(toc)
    n = sum(toc.audio)
    return f"{id1 & 0xF:x}/{id1 >> 4 & 0xF:x}/{id1 >> 8 & 0xF:x}/dBAR-{n:03d}-{id1:08x}-{id2:08x}-{cddb:08x}.bin"


def parse_ar(data: bytes) -> list[dict]:
    """The dBAR file: records per submission group, each with (confidence, crc, frame450) per track."""
    pos, discs = 0, []
    while pos + 13 <= len(data):
        n = data[pos]
        id1, id2, cddb = struct.unpack_from("<III", data, pos + 1)
        pos += 13
        tracks = []
        for _ in range(n):
            if pos + 9 > len(data):
                break
            conf = data[pos]
            crc, crc450 = struct.unpack_from("<II", data, pos + 1)
            pos += 9
            tracks.append({"confidence": conf, "crc": crc, "crc450": crc450})
        discs.append({"id1": id1, "id2": id2, "cddb": cddb, "tracks": tracks})
    return discs


def ar_checksums(words: np.ndarray, is_first: bool, is_last: bool) -> tuple[int, int]:
    """v1 and v2 of one track's samples (uint32 words, L | R << 16)."""
    n = len(words)
    lo = AR_SKIP_FIRST if is_first else 0
    hi = n - AR_SKIP_LAST if is_last else n
    if hi <= lo:
        return 0, 0
    idx = np.arange(lo + 1, hi + 1, dtype=np.uint64)
    prod = words[lo:hi].astype(np.uint64) * idx
    v1 = int((prod & np.uint64(0xFFFFFFFF)).sum()) & 0xFFFFFFFF
    v2 = (v1 + int((prod >> np.uint64(32)).sum())) & 0xFFFFFFFF
    return v1, v2


def frame450_all_offsets(words: np.ndarray, prev_tail: np.ndarray, next_head: np.ndarray) -> dict[int, int]:
    """The frame-450 sum for every pressing offset in the searched range: offset -> value."""
    base = 450 * SECTOR
    if len(words) < base + SECTOR:
        return {}
    # Samples base-R .. base+588+R of the disc, zero-padded with the neighbours' edges.
    joined = np.concatenate([prev_tail, words, next_head]).astype(np.uint64)
    shift = len(prev_tail)
    out = {}
    weights = np.arange(1, SECTOR + 1, dtype=np.uint64)
    lo = base - AR_OFFSET_RANGE + shift
    window = joined[lo: base + SECTOR + AR_OFFSET_RANGE + shift]
    if len(window) < SECTOR + 2 * AR_OFFSET_RANGE:
        return {}
    # Sliding weighted sums via cumulative sums: S(o) = sum(w[i] * x[o+i]) = sum((i+1) x[o+i]).
    # sum(i*x[o+i]) for i in 0..587 = sum over k of x[o+k]*k: use two cumulative sums.
    x = window
    zero = np.zeros(1, dtype=np.uint64)  # a Python 0 would turn the array into float64
    cs = np.concatenate([zero, np.cumsum(x, dtype=np.uint64)])
    kx = x * np.arange(len(x), dtype=np.uint64)
    cks = np.concatenate([zero, np.cumsum(kx, dtype=np.uint64)])
    for o in range(2 * AR_OFFSET_RANGE + 1):
        # sum_{i=0}^{587} (i+1) x[o+i] = sum k x[k] for k in [o, o+588) - (o-1) * sum x[k]
        s_kx = int(cks[o + SECTOR] - cks[o])
        s_x = int(cs[o + SECTOR] - cs[o])
        out[o - AR_OFFSET_RANGE] = (s_kx - (o - 1) * s_x) & 0xFFFFFFFF
    return out


def shifted(words: np.ndarray, prev_tail: np.ndarray, next_head: np.ndarray, oi: int) -> np.ndarray:
    """The track's window moved by oi samples, borrowing from the neighbours (zeros beyond)."""
    if oi == 0:
        return words
    n = len(words)
    joined = np.concatenate([prev_tail, words, next_head])
    lo = len(prev_tail) + oi
    hi = lo + n
    pad_lo, pad_hi = max(0, -lo), max(0, hi - len(joined))
    core = joined[max(lo, 0): min(hi, len(joined))]
    if pad_lo or pad_hi:
        core = np.concatenate([np.zeros(pad_lo, dtype=np.uint32), core, np.zeros(pad_hi, dtype=np.uint32)])
    return core


# CUETools DB ---------------------------------------------------------------------------------

def parse_ctdb(text: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    entries = []
    for el in root.iter():
        if el.tag.split("}")[-1] != "entry":
            continue
        crcs = (el.get("trackcrcs") or "").split()
        try:
            entries.append({
                "id": el.get("id"), "crc32": int(el.get("crc32") or "0", 16), "confidence": int(el.get("confidence") or 0),
                "trackcrcs": [int(c, 16) for c in crcs], "toc": el.get("toc") or "", "hasparity": bool(el.get("hasparity")),
            })
        except ValueError:
            continue
    return entries


def ctdb_bounds(total_samples: int) -> tuple[int, int]:
    """Samples dropped at the start and at the end of the disc for the CTDB CRC."""
    laststride = CTDB_STRIDE + (total_samples * 2) % CTDB_STRIDE
    return CTDB_PREFIX, laststride // 2


# Decoding ------------------------------------------------------------------------------------

async def decode_track(path: Path) -> np.ndarray:
    """The file as CD audio: uint32 words (left | right << 16), via ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise RuntimeError("ffmpeg is not installed")
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "2", "-ar", "44100", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 600)
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace").strip() or f"ffmpeg exited {proc.returncode}")
    return np.frombuffer(out[: len(out) - len(out) % 4], dtype="<u4").copy()


def cd_audio_info(path: Path) -> tuple[int | None, int | None, int | None, int | None]:
    """(bits, rate, channels, total samples) from the file's header."""
    try:
        info = mutagen.File(path)
    except Exception:  # noqa: BLE001
        return None, None, None, None
    i = getattr(info, "info", None)
    if i is None:
        return None, None, None, None
    bits = getattr(i, "bits_per_sample", None)
    rate = getattr(i, "sample_rate", None)
    channels = getattr(i, "channels", None)
    total = getattr(i, "total_samples", None)
    if total is None and rate and getattr(i, "length", None):
        total = int(round(i.length * rate))
    return bits, rate, channels, total


# Lookups -------------------------------------------------------------------------------------

def _agent(settings: Settings) -> str:
    return f"SoulseekPicker/{__version__} ( {settings.mb_contact or 'unconfigured'} )"


async def fetch_accuraterip(toc: Toc, settings: Settings) -> list[dict] | None:
    """The disc's records, [] when the disc is not in the database, None when the lookup failed."""
    path = ar_path(toc)
    cached = cache_get(f"ar:{path}", LOOKUP_TTL)
    if cached is not None:
        return cached if cached != "absent" else []
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _agent(settings)}) as client:
            resp = await client.get(f"{AR_BASE}/{path}")
    except httpx.HTTPError:
        return None
    if resp.status_code == 404:
        cache_put(f"ar:{path}", "absent")
        return []
    if resp.status_code != 200:
        return None
    discs = parse_ar(resp.content)
    cache_put(f"ar:{path}", discs)
    return discs


async def fetch_ctdb(toc: Toc, settings: Settings) -> list[dict] | None:
    key = f"ctdb:{toc.ctdb_string()}"
    cached = cache_get(key, LOOKUP_TTL)
    if cached is not None:
        return cached if cached != "absent" else []
    params = {"version": "3", "ctdb": "1", "fuzzy": "1", "metadata": "none", "toc": toc.ctdb_string()}
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": _agent(settings)}) as client:
            resp = await client.get(CTDB_URL, params=params)
    except httpx.HTTPError:
        return None
    if resp.status_code == 404:
        cache_put(key, "absent")
        return []
    if resp.status_code != 200:
        return None
    entries = parse_ctdb(resp.text)
    cache_put(key, entries)
    return entries


# The check -----------------------------------------------------------------------------------

@dataclass
class TrackScan:
    file: str
    samples: int
    v1: int
    v2: int
    frame450: dict[int, int]
    ar_at: dict[int, int] = field(default_factory=dict)  # offset -> v1 for the offsets tried
    ctdb_crc: int = 0
    ctdb_at: dict[int, int] = field(default_factory=dict)


def _expected_sectors(toc: Toc) -> list[int]:
    return toc.audio_sectors


async def scan_disc(files: list[Path], toc: Toc, offsets_to_try: set[int], decode=None) -> tuple[list[TrackScan], dict[int, int]]:
    """Decode the tracks one by one and compute every checksum at the requested offsets.

    Returns the per-track scans and the CTDB disc CRC per offset.
    """
    decode = decode or decode_track
    sectors = _expected_sectors(toc)
    total = sum(sectors) * SECTOR
    prefix, suffix = ctdb_bounds(total)
    n = len(files)
    zeros = np.zeros(AR_OFFSET_RANGE, dtype=np.uint32)
    scans: list[TrackScan] = []
    disc_crc = {oi: 0 for oi in offsets_to_try | {0}}
    disc_pos = 0  # samples before the current track

    async def padded(i: int) -> np.ndarray:
        words = await decode(files[i])
        want = sectors[i] * SECTOR
        if len(words) < want:
            words = np.concatenate([words, np.zeros(want - len(words), dtype=np.uint32)])
        elif len(words) > want:
            words = words[:want]
        return words

    prev_tail = zeros
    current = await padded(0)
    for i in range(n):
        nxt = await padded(i + 1) if i + 1 < n else None
        next_head = nxt[:AR_OFFSET_RANGE] if nxt is not None else zeros
        if len(next_head) < AR_OFFSET_RANGE:
            next_head = np.concatenate([next_head, np.zeros(AR_OFFSET_RANGE - len(next_head), dtype=np.uint32)])
        first, last = i == 0, i == n - 1
        v1, v2 = ar_checksums(current, first, last)
        scan = TrackScan(files[i].name, len(current), v1, v2, frame450_all_offsets(current, prev_tail, next_head))
        a = prefix if first else 0
        b = len(current) - suffix if last else len(current)
        for oi in sorted(offsets_to_try | {0}):
            win = shifted(current, prev_tail, next_head, oi)
            if oi != 0:
                scan.ar_at[oi] = ar_checksums(win, first, last)[0]
            piece = win[a:b].astype("<u4").tobytes() if b > a else b""
            crc = zlib.crc32(piece)
            scan.ctdb_at[oi] = crc
            disc_crc[oi] = zlib.crc32(piece, disc_crc[oi])
        scan.ctdb_crc = scan.ctdb_at[0]
        scans.append(scan)
        disc_pos += len(current)
        prev_tail = current[-AR_OFFSET_RANGE:] if len(current) >= AR_OFFSET_RANGE else np.concatenate(
            [np.zeros(AR_OFFSET_RANGE - len(current), dtype=np.uint32), current])
        current = nxt
    return scans, disc_crc


def candidate_offsets(scans_frame450: list[dict[int, int]], records: list[dict]) -> set[int]:
    """Pressing offsets at which the frame-450 sums match the database for most tracks."""
    votes: dict[int, int] = {}
    for t, f450 in enumerate(scans_frame450):
        wanted = {rec["tracks"][t]["crc450"] for rec in records if t < len(rec["tracks"]) and rec["tracks"][t]["crc450"]}
        if not wanted or not f450:
            continue
        for oi, value in f450.items():
            if value in wanted:
                votes[oi] = votes.get(oi, 0) + 1
    if not votes:
        return set()
    best = max(votes.values())
    return {oi for oi, v in votes.items() if v == best and oi != 0}


def judge_accuraterip(scans: list[TrackScan], records: list[dict]) -> dict:
    """Per track: confidence of matching records (v1 at the best offset, or v2 at 0) and the total known."""
    if not records:
        return {"status": "not_in_db", "tracks": [], "offset": 0, "confidence": 0}
    offsets = {0} | {oi for s in scans for oi in s.ar_at}
    best_oi, best_matches = 0, -1
    per_offset: dict[int, list[dict]] = {}
    for oi in sorted(offsets, key=abs):
        rows = []
        for t, s in enumerate(scans):
            conf = total = 0
            for rec in records:
                if t >= len(rec["tracks"]):
                    continue
                r = rec["tracks"][t]
                total += r["confidence"]
                mine = s.v1 if oi == 0 else s.ar_at.get(oi)
                if r["crc"] and (mine == r["crc"] or (oi == 0 and s.v2 == r["crc"])):
                    conf += r["confidence"]
            rows.append({"n": t + 1, "file": s.file, "v1": f"{(s.v1 if oi == 0 else s.ar_at.get(oi, 0)):08x}", "v2": f"{s.v2:08x}",
                         "confidence": conf, "total": total,
                         "status": "ok" if conf else ("not_in_db" if not total else "mismatch")})
        per_offset[oi] = rows
        matches = sum(1 for r in rows if r["status"] == "ok")
        if matches > best_matches:
            best_oi, best_matches = oi, matches
    rows = per_offset[best_oi]
    known = [r for r in rows if r["total"]]
    if not known:
        status = "not_in_db"
    elif all(r["status"] == "ok" for r in known):
        status = "verified"
    elif any(r["status"] == "ok" for r in known):
        status = "mismatch"
    else:
        status = "no_match"
    return {"status": status, "tracks": rows, "offset": best_oi,
            "confidence": min((r["confidence"] for r in rows if r["status"] == "ok"), default=0),
            "matched": best_matches, "known": len(known)}


def judge_ctdb(scans: list[TrackScan], disc_crc: dict[int, int], entries: list[dict], toc: Toc) -> dict:
    if not entries:
        return {"status": "not_in_db", "tracks": [], "offset": 0, "confidence": 0, "entries": 0}
    mine_toc = toc.ctdb_string()
    best = None
    for oi, crc in disc_crc.items():
        conf = sum(e["confidence"] for e in entries if e["crc32"] == crc)
        if conf and (best is None or conf > best[1]):
            best = (oi, conf)
    offset = best[0] if best else 0
    rows = []
    for t, s in enumerate(scans):
        mine = s.ctdb_at.get(offset, s.ctdb_crc)
        conf = sum(e["confidence"] for e in entries if t < len(e["trackcrcs"]) and e["trackcrcs"][t] == mine)
        total = sum(e["confidence"] for e in entries if t < len(e["trackcrcs"]))
        rows.append({"n": t + 1, "file": s.file, "crc": f"{mine:08x}", "confidence": conf, "total": total,
                     "status": "ok" if conf else ("not_in_db" if not total else "mismatch")})
    if best:
        status = "verified"
    elif any(r["status"] == "ok" for r in rows):
        status = "mismatch" if any(r["status"] == "mismatch" for r in rows) else "verified_tracks"
    else:
        status = "no_match"
    return {"status": status, "tracks": rows, "offset": offset, "confidence": best[1] if best else 0,
            "entries": len(entries), "same_toc": sum(1 for e in entries if e["toc"] == mine_toc)}


def _disc_groups(files: list[Path]) -> list[list[Path]]:
    groups: dict[Path, list[Path]] = {}
    for f in files:
        groups.setdefault(f.parent, []).append(f)
    return [sorted(v, key=lambda p: p.name.lower()) for _, v in sorted(groups.items())]


def _log_tocs(root: Path) -> list[Toc]:
    from .riplog import parse_tocs, decode as decode_text
    tocs = []
    for log in sorted(root.rglob("*.log")):
        try:
            text = decode_text(log.read_bytes())
        except OSError:
            continue
        for entries in parse_tocs(text):
            tocs.append(toc_from_log(entries))
    return tocs


async def verify_folder(root: Path, files: list[Path], settings: Settings, *, decode=None,
                        fetch_ar=None, fetch_ctdb=None) -> dict:
    """Every disc in the folder against both databases. Never raises."""
    fetch_ar = fetch_ar or fetch_accuraterip
    fetch_ctdb = fetch_ctdb or fetch_ctdb_entries
    result: dict = {"applicable": True, "discs": [], "mismatched": [], "summary": ""}
    log_tocs = _log_tocs(root)
    for disc_files in _disc_groups(files):
        infos = [cd_audio_info(p) for p in disc_files]
        if any(b not in (16, None) or r not in (44100, None) or ch not in (2, None) for b, r, ch, _ in infos):
            result["discs"].append({"applicable": False, "reason": "not CD audio (16-bit 44.1 kHz stereo)", "files": len(disc_files)})
            continue
        if any(t is None for *_, t in infos):
            result["discs"].append({"applicable": False, "reason": "a track's length is unknown", "files": len(disc_files)})
            continue
        samples = [t for *_, t in infos]
        toc = next((t for t in log_tocs if sum(t.audio) == len(disc_files)
                    and all(abs(sec * SECTOR - n) < SECTOR for sec, n in zip(t.audio_sectors, samples))), None)
        if toc is None:
            toc = toc_from_lengths(samples)
        if toc is None:
            result["discs"].append({"applicable": False, "reason": "no usable table of contents", "files": len(disc_files)})
            continue
        disc: dict = {"applicable": True, "toc_source": toc.source, "toc": toc.ctdb_string(), "files": len(disc_files)}
        try:
            ar_records = await fetch_ar(toc, settings) if settings.accuraterip else []
            ctdb_entries = await fetch_ctdb(toc, settings)
            try_offsets: set[int] = set()
            # First pass at offset 0 to learn the pressing offset from the frame-450 sums, then
            # a second pass only when the database suggests another offset.
            scans, disc_crc = await scan_disc(disc_files, toc, set(), decode)
            if ar_records:
                try_offsets = candidate_offsets([s.frame450 for s in scans], ar_records)
            if try_offsets:
                scans, disc_crc = await scan_disc(disc_files, toc, try_offsets, decode)
            disc["accuraterip"] = judge_accuraterip(scans, ar_records or []) if settings.accuraterip and ar_records is not None \
                else {"status": "off" if not settings.accuraterip else "error", "tracks": [], "offset": 0, "confidence": 0}
            disc["ctdb"] = judge_ctdb(scans, disc_crc, ctdb_entries or [], toc) if ctdb_entries is not None \
                else {"status": "error", "tracks": [], "offset": 0, "confidence": 0, "entries": 0}
        except Exception as exc:  # noqa: BLE001 - verification must never stop an import
            disc["applicable"] = False
            disc["reason"] = f"could not verify: {exc}"
        result["discs"].append(disc)
        for key in ("accuraterip", "ctdb"):
            j = disc.get(key) or {}
            if j.get("status") == "mismatch":
                for row in j["tracks"]:
                    if row["status"] == "mismatch" and row["file"] not in result["mismatched"]:
                        result["mismatched"].append(row["file"])
    result["applicable"] = any(d.get("applicable") for d in result["discs"])
    result["summary"] = summarize(result)
    return result


async def fetch_ctdb_entries(toc: Toc, settings: Settings) -> list[dict] | None:
    return await fetch_ctdb(toc, settings)


def summarize(result: dict) -> str:
    parts = []
    for i, d in enumerate(result["discs"], 1):
        label = f"disc {i}: " if len(result["discs"]) > 1 else ""
        if not d.get("applicable"):
            parts.append(f"{label}{d.get('reason', 'not checked')}")
            continue
        ar, ct = d.get("accuraterip") or {}, d.get("ctdb") or {}
        bits = []
        if ct.get("status") == "verified":
            bits.append(f"CTDB verified (confidence {ct['confidence']}" + (f", offset {ct['offset']:+d}" if ct.get("offset") else "") + ")")
        elif ct.get("status") in ("mismatch", "no_match", "verified_tracks"):
            ok = sum(1 for r in ct["tracks"] if r["status"] == "ok")
            bits.append(f"CTDB {ok}/{len(ct['tracks'])} tracks match")
        elif ct.get("status") == "not_in_db":
            bits.append("CTDB: not in database")
        elif ct.get("status") == "error":
            bits.append("CTDB: lookup failed")
        if ar.get("status") == "verified":
            bits.append(f"AccurateRip {ar['matched']}/{ar['known']} (confidence {ar['confidence']}" + (f", offset {ar['offset']:+d}" if ar.get("offset") else "") + ")")
        elif ar.get("status") in ("mismatch", "no_match"):
            bits.append(f"AccurateRip {ar['matched']}/{ar['known']} tracks accurate")
        elif ar.get("status") == "not_in_db":
            bits.append("AccurateRip: not in database")
        elif ar.get("status") == "error":
            bits.append("AccurateRip: lookup failed")
        elif ar.get("status") == "off":
            bits.append("AccurateRip off")
        parts.append(label + ", ".join(bits))
    return "; ".join(parts)


def verdict(result: dict | None) -> str:
    """verified | mismatch | no_match | not_in_db | not_applicable | none."""
    if not result:
        return "none"
    if not result.get("applicable"):
        return "not_applicable"
    if result.get("mismatched"):
        return "mismatch"
    statuses = {(d.get(k) or {}).get("status") for d in result["discs"] if d.get("applicable") for k in ("accuraterip", "ctdb")}
    if "verified" in statuses or "verified_tracks" in statuses:
        return "verified"
    if "no_match" in statuses:
        return "no_match"
    return "not_in_db"


TRACK_NUMBER = re.compile(r"^\s*(\d{1,3})")
