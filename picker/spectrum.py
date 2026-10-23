"""Transcode detection: where does the audio's spectrum end?

A FLAC made from an MP3 or AAC decodes fine and passes `flac -t`, but lossy encoders cut
everything above a low-pass frequency (LAME: ~16 kHz at 128 kbps, ~19 kHz at 192, ~20 kHz at
256-320 and V0). The cut shows as a cliff in the spectrum: a drop of tens of dB within a few
hundred Hz, with almost nothing above. Real recordings roll off gradually instead.

Some masterings really are band-limited (old tape transfers, early digital, deliberate
low-pass filters), so a cliff is a *warning* for the Review page, never a reject.

`analyse()` is pure: mono float samples in, a verdict out. `analyse_file()` decodes three
10-second windows with ffmpeg and combines them.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict, dataclass

import numpy as np

FRAME = 8192  # samples per FFT frame
SLICE_HZ = 250  # width of the slices the spectrum is summarised in
SPAN_HZ = 1000  # compare this much spectrum below a candidate cliff with this much above
REFERENCE = (1000, 14000)  # the band music always has energy in
SILENCE_DB = -75  # reference level (dBFS-ish) below which nothing can be judged
WINDOW_S = 10
POSITIONS = (0.15, 0.5, 0.85)  # where in the track the windows start

# (upper bound of the cutoff in Hz, what it is consistent with); approximate by design
LOSSY_HINTS = [
    (15500, "a low-bitrate lossy source (below 128 kbps)"),
    (17000, "a ~128 kbps lossy source"),
    (18500, "a ~160-192 kbps lossy source"),
    (19700, "a ~192-256 kbps or V0-V2 lossy source"),
    (20800, "a ~256-320 kbps lossy source"),
]


@dataclass
class Verdict:
    kind: str  # full | lossy | upsampled | unknown
    cutoff_hz: float | None  # where the spectrum ends, when there is a cliff
    drop_db: float | None  # size of the cliff
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def spectrum_db(samples: np.ndarray, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Average power spectrum (dB) of mono samples, Hann-windowed frames with 50% overlap."""
    samples = np.asarray(samples, dtype=np.float64)
    if len(samples) < FRAME:
        samples = np.pad(samples, (0, FRAME - len(samples)))
    hop = FRAME // 2
    window = np.hanning(FRAME)
    starts = range(0, len(samples) - FRAME + 1, hop)
    power = np.zeros(FRAME // 2 + 1)
    for n, start in enumerate(starts, 1):
        power += np.abs(np.fft.rfft(samples[start:start + FRAME] * window)) ** 2
    power /= max(1, n)
    freqs = np.fft.rfftfreq(FRAME, 1 / rate)
    return freqs, 10 * np.log10(power + 1e-20)


def slices(freqs: np.ndarray, db: np.ndarray, start: float, stop: float) -> tuple[np.ndarray, np.ndarray]:
    """Mean level (dB) in SLICE_HZ-wide slices from start to stop."""
    edges = np.arange(start, stop + 1, SLICE_HZ)
    centers, levels = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (freqs >= lo) & (freqs < hi)
        if mask.any():
            centers.append((lo + hi) / 2)
            levels.append(float(np.mean(db[mask])))
    return np.array(centers), np.array(levels)


def lossy_hint(cutoff: float) -> str:
    for bound, text in LOSSY_HINTS:
        if cutoff < bound:
            return text
    return "a high-bitrate lossy source, or a deliberate low-pass"


def analyse(samples: np.ndarray, rate: int, cutoff_db: float = 30.0) -> Verdict:
    freqs, db = spectrum_db(samples, rate)
    ref_mask = (freqs >= REFERENCE[0]) & (freqs < REFERENCE[1])
    reference = float(np.mean(db[ref_mask]))
    if reference < SILENCE_DB:
        return Verdict("unknown", None, None, "too quiet to judge")

    nyquist = rate / 2
    centers, levels = slices(freqs, db, 10000, nyquist)
    per_span = SPAN_HZ // SLICE_HZ
    best_drop, best_at = 0.0, None
    for i in range(per_span, len(levels) - 1):
        below = float(np.mean(levels[i - per_span:i]))
        above = levels[i:i + per_span]
        rest = levels[i:]
        # A cliff: the next kHz is far below the last, and nothing above climbs back.
        drop = below - float(np.mean(above))
        if drop > best_drop and below - float(np.max(rest)) >= cutoff_db * 0.66:
            best_drop, best_at = drop, float(centers[i] - SLICE_HZ / 2)

    if best_at is None or best_drop < cutoff_db or best_at > nyquist - SPAN_HZ:
        if rate > 48000:
            high = levels[centers > 23000]
            if len(high) and reference - float(np.mean(high)) > 80:
                return Verdict("upsampled", None, None,
                               f"{rate / 1000:g} kHz file with no content above ~22 kHz: probably upsampled from 44.1/48 kHz")
        return Verdict("full", None, None, "energy continues to the top of the spectrum")

    cutoff = round(best_at / 100) * 100
    if rate > 48000 and cutoff >= 20800:
        return Verdict("upsampled", cutoff, round(best_drop, 1),
                       f"{rate / 1000:g} kHz file ending at {cutoff / 1000:.1f} kHz: probably upsampled from 44.1/48 kHz")
    return Verdict("lossy", cutoff, round(best_drop, 1),
                   f"spectrum ends at {cutoff / 1000:.1f} kHz ({best_drop:.0f} dB cliff), consistent with {lossy_hint(cutoff)}")


def combine(verdicts: list[Verdict]) -> Verdict:
    """One verdict for a file or an album: the most suspicious wins, the lowest cutoff among lossy."""
    rank = {"lossy": 3, "upsampled": 2, "full": 1, "unknown": 0}
    known = [v for v in verdicts if v.kind != "unknown"]
    if not known:
        return Verdict("unknown", None, None, "too quiet to judge")
    worst = max(rank[v.kind] for v in known)
    same = [v for v in known if rank[v.kind] == worst]
    if worst == 3:
        return min(same, key=lambda v: v.cutoff_hz or 0)
    return same[0]


async def decode_window(path: str, start: float, rate: int, seconds: float = WINDOW_S) -> np.ndarray:
    """Mono float32 samples from ffmpeg, at the file's own rate."""
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise RuntimeError("ffmpeg is not installed")
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}", "-i", path,
        "-ac", "1", "-ar", str(rate), "-f", "f32le", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 120)
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace").strip() or f"ffmpeg exited {proc.returncode}")
    return np.frombuffer(out, dtype=np.float32)


async def analyse_file(path: str, rate: int, length: float, cutoff_db: float = 30.0) -> Verdict:
    """Three windows (early, middle, late); the file's verdict is the most suspicious of them."""
    if length <= WINDOW_S * 2:
        starts = [0.0]
    else:
        starts = [max(0.0, min(length - WINDOW_S, length * p)) for p in POSITIONS]
    verdicts = []
    for start in starts:
        samples = await decode_window(path, start, rate)
        verdicts.append(analyse(samples, rate, cutoff_db))
    return file_verdict(verdicts)


def file_verdict(windows: list[Verdict]) -> Verdict:
    """A file is called lossy or upsampled only when most of its windows agree, so one odd
    stretch (a quiet intro, a filtered breakdown) doesn't condemn it."""
    known = [v for v in windows if v.kind != "unknown"]
    for kind in ("lossy", "upsampled"):
        hits = [v for v in known if v.kind == kind]
        if hits and len(hits) * 2 > len(known):
            return combine(hits)
    full = [v for v in known if v.kind == "full"]
    return full[0] if full else combine(windows)
