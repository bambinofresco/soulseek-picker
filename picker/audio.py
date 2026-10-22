"""Integrity checks and spectrograms for a finished download."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import mutagen

from .grouping import AUDIO_EXTS

TOOL_TIMEOUT = 300  # seconds per file


async def run_tool(args: list[str], timeout: float = TOOL_TIMEOUT) -> tuple[int, str]:
    """Run a command; (exit code, combined output). 127 when the tool is missing."""
    exe = shutil.which(args[0])
    if exe is None:
        return 127, f"{args[0]} is not installed"
    proc = await asyncio.create_subprocess_exec(
        exe, *args[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"{args[0]} timed out after {timeout:.0f} s"
    return proc.returncode, out.decode("utf-8", "replace").strip()


def audio_files(root: Path) -> list[Path]:
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO_EXTS),
        key=lambda p: str(p.relative_to(root)).lower(),
    )


async def check_file(path: Path) -> dict:
    """flac -t for FLAC files, and mutagen must be able to read every audio file."""
    name = path.name
    if path.suffix.lower() == ".flac":
        code, out = await run_tool(["flac", "-t", "-s", str(path)])
        if code != 0:
            return {"name": name, "path": str(path), "ok": False, "detail": out.splitlines()[-1] if out else f"flac -t exited {code}"}
    try:
        info = mutagen.File(path)
    except Exception as exc:  # noqa: BLE001 - mutagen raises many types on bad files
        return {"name": name, "path": str(path), "ok": False, "detail": f"unreadable: {exc}"}
    if info is None:
        return {"name": name, "path": str(path), "ok": False, "detail": "unreadable: unknown format"}
    length = getattr(info.info, "length", None)
    return {"name": name, "path": str(path), "ok": True, "detail": f"{length:.0f} s" if length else "ok", "length": length}


async def verify(root: Path, expected_count: int) -> tuple[bool, list[dict], str | None]:
    """(all good, per-file results, reason for failure)."""
    files = [p for p in root.rglob("*") if p.is_file()]
    if len(files) < expected_count:
        return False, [], f"expected {expected_count} files, found {len(files)} in {root}"
    results = [await check_file(p) for p in audio_files(root)]
    if not results:
        return False, [], f"no audio files in {root}"
    bad = [r for r in results if not r["ok"]]
    if bad:
        return False, results, f"{len(bad)} file(s) failed the integrity check, e.g. {bad[0]['name']}: {bad[0]['detail']}"
    return True, results, None


EDITION_TAGS = [
    "album", "albumartist", "date", "originaldate", "label", "catalognumber", "releasecountry", "media",
    "barcode", "releasestatus", "musicbrainz_albumid", "musicbrainz_releasegroupid", "musicbrainz_albumcomment",
]


def tag_snapshot(album_dir: Path) -> dict:
    """The edition tags of the first track, as written to disk (Vorbis comment names).

    Shown on the review page and in History, to check beets wrote the chosen edition.
    """
    files = audio_files(album_dir)
    if not files:
        return {"file": None, "tags": {}, "error": f"no audio files in {album_dir}"}
    try:
        info = mutagen.File(files[0], easy=False)
    except Exception as exc:  # noqa: BLE001 - mutagen raises many types on bad files
        return {"file": files[0].name, "tags": {}, "error": str(exc)}
    tags = {}
    raw = getattr(info, "tags", None) or {}
    for key in EDITION_TAGS:
        values = raw.get(key) or raw.get(key.upper())
        if values:
            tags[key] = "; ".join(str(v) for v in (values if isinstance(values, list) else [values]))
    return {"file": str(files[0].relative_to(album_dir)), "tags": tags, "error": None}


def spectrogram_picks(results: list[dict], files: list[Path]) -> list[Path]:
    """The longest track and one from the middle."""
    if not files:
        return []
    lengths = {r["name"]: r.get("length") or 0 for r in results}
    longest = max(files, key=lambda p: lengths.get(p.name, 0))
    middle = files[len(files) // 2]
    return [longest] if middle == longest else [longest, middle]


async def spectrograms(paths: list[Path], out_dir: Path) -> tuple[list[str], str | None]:
    """Render with sox; returns (png file names, error)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i, path in enumerate(paths, 1):
        png = out_dir / f"{i}.png"
        if not png.exists():
            code, out = await run_tool(["sox", str(path), "-n", "spectrogram", "-t", path.name, "-o", str(png)])
            if code != 0:
                return names, out or f"sox exited {code}"
        names.append(png.name)
    return names, None
