"""Run beets as a subprocess to tag an album folder in place, pinned to one release.

beets reads its base config from $BEETSDIR/config.yaml (generated on first run, then
yours to edit). `-c` takes a single file that beets layers on top of that base, which is
how Force apply raises strong_rec_thresh without touching the base config.

In quiet mode beets *skips* an album whose match is not strong, even with --search-id.
Rather than parse its output, the result is judged by asking the beets library whether
the release is now there at that folder.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Settings

BASE_CONFIG = """\
directory: {music_dir}
library: {beets_dir}/library.db
import:
  copy: no
  move: no
  write: yes
  quiet: yes
  quiet_fallback: skip
  duplicate_action: keep
  timid: no
  log: {beets_dir}/import.log
match:
  # beets' default is 0.04; looser because the release is already pinned. Tune against real imports.
  strong_rec_thresh: 0.10
musicbrainz:
  ratelimit: 1
# musicbrainz is the autotagger itself in beets 2.x: a plugins list without it finds no candidates.
plugins: musicbrainz fetchart
fetchart:
  auto: yes
"""
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
FORCE_OVERLAY = """\
# Written by the picker for Force apply: accept the pinned release whatever the distance.
# beets tests distance < strong_rec_thresh strictly and distances run 0..1, so 1.0 would
# still skip the worst case; and max_rec would cap a forced match with missing or
# unmatched tracks at medium, which quiet mode also skips.
match:
  strong_rec_thresh: 1.01
  max_rec:
    missing_tracks: strong
    unmatched_tracks: strong
"""


@dataclass
class BeetsResult:
    imported: bool
    skipped: bool
    exit_code: int
    output: str


def ensure_config(settings: Settings) -> Path:
    """Create the beets config on first run; never overwrite one that exists."""
    beets_dir = settings.beets_dir
    beets_dir.mkdir(parents=True, exist_ok=True)
    config = beets_dir / "config.yaml"
    if not config.exists():
        config.write_text(
            BASE_CONFIG.format(music_dir=settings.music_dir.as_posix(), beets_dir=beets_dir.as_posix()),
            encoding="utf-8",
        )
    (beets_dir / "force.yaml").write_text(FORCE_OVERLAY, encoding="utf-8")
    return config


def config_warnings(settings: Settings) -> list[str]:
    """Problems in the (user-editable) beets config that would make every import fail."""
    path = settings.beets_dir / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [f"Cannot read the beets config at {path}: {exc}"] if path.exists() else []
    plugins = data.get("plugins", ["musicbrainz"])
    plugins = plugins.split() if isinstance(plugins, str) else list(plugins or [])
    warnings = []
    if "musicbrainz" not in plugins:
        warnings.append(f"The beets config at {path} does not load the musicbrainz plugin, so beets finds no "
                        "candidates and skips every import. Add musicbrainz to its plugins line.")
    import_cfg = data.get("import") or {}
    if import_cfg.get("move") or import_cfg.get("copy"):
        warnings.append(f"The beets config at {path} sets import move or copy; the picker places folders itself. Set both to no.")
    return warnings


async def _beet(settings: Settings, args: list[str], timeout: float) -> tuple[int, str]:
    env = {**os.environ, "BEETSDIR": str(settings.beets_dir), "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "beets", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"beets timed out after {timeout / 60:.0f} minutes"
    text = ANSI_ESCAPE.sub("", out.decode("utf-8", "replace"))
    # beets announces each one-time database migration; keep them out of the import output.
    lines = [line for line in text.splitlines() if not line.startswith("Created database backup at:")]
    return proc.returncode, "\n".join(lines).strip()


def _same_dir(a: str, b: Path) -> bool:
    try:
        return Path(a).resolve() == b.resolve()
    except OSError:
        return False


async def album_paths(settings: Settings, release_mbid: str) -> list[str]:
    """Library folders beets holds for this release."""
    code, out = await _beet(settings, ["ls", "-a", f"mb_albumid:{release_mbid}", "-f", "$path"], 60)
    return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []


async def forget_album(settings: Settings, release_mbid: str, album_dir: Path) -> None:
    """Drop the library entry (never the files) so a retag starts clean."""
    for path in await album_paths(settings, release_mbid):
        if _same_dir(path, album_dir):
            await _beet(settings, ["remove", "-a", "-f", f"mb_albumid:{release_mbid}", f"path:{album_dir}"], 60)
            return


async def import_album(
    settings: Settings, album_dir: Path, release_mbid: str, *, force: bool = False, pretend: bool = False,
) -> BeetsResult:
    ensure_config(settings)
    args = ["-c", str(settings.beets_dir / "force.yaml")] if force else []
    args += ["import", "-q", "--search-id", release_mbid]
    if pretend:
        args.append("--pretend")
    args.append(str(album_dir))
    code, out = await _beet(settings, args, settings.beets_timeout_minutes * 60)
    if pretend:
        return BeetsResult(imported=code == 0, skipped=False, exit_code=code, output=out)
    imported = code == 0 and any(_same_dir(p, album_dir) for p in await album_paths(settings, release_mbid))
    skipped = not imported and "Skipping" in out
    return BeetsResult(imported=imported, skipped=skipped, exit_code=code, output=out)
