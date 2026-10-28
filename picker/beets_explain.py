"""Why beets skipped an album: its own distance breakdown for the pinned release, as JSON.

Run as a subprocess by picker.beets.explain_skip (beets' config and plugins are process-wide,
so it never runs inside the app):

    python -m picker.beets_explain <beets_dir> <overlay.yaml> <release_mbid> <album_dir>

It rates the folder against the release the way `beet import --search-id` does, with the same
config and overlay, and prints one JSON object: the similarity, the threshold it needed, the
album-level penalties, and for every file the track it was paired with and what differs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

AUDIO_EXTS = {"flac", "mp3", "m4a", "ogg", "opus", "wav", "aiff", "ape", "wv", "alac", "aac"}

PENALTY_NAMES = {
    "artist": "artist name",
    "album": "album title",
    "tracks": "track differences (below)",
    "missing_tracks": "tracks of the release with no file",
    "unmatched_tracks": "files that match no track",
    "track_title": "title",
    "track_length": "length",
    "track_index": "track number",
    "track_artist": "track artist",
    "track_id": "recording ID",
    "medium": "disc number",
    "mediums": "disc count",
    "media": "format",
    "year": "year",
    "country": "country",
    "label": "label",
    "catalognum": "catalog number",
    "albumdisambig": "disambiguation",
    "album_id": "release ID",
    "source": "data source",
}


def _audio_files(root: Path) -> list[Path]:
    return sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO_EXTS),
                  key=lambda p: str(p.relative_to(root)).lower())


def _penalties(dist) -> list[dict]:
    return [{"key": key, "name": PENALTY_NAMES.get(key, key), "distance": round(value, 3)} for key, value in dist.items()]


def explain(items, album_info, threshold: float) -> dict:
    """The breakdown for one candidate release, built the way beets' matcher builds it."""
    from beets.autotag import Source, assign_items, distance

    source = Source.from_items(items)
    pairs, extra_items, extra_tracks = assign_items(items, album_info.tracks)
    dist = distance(source.data, album_info, pairs, len(extra_items))
    tracks = []
    for item, info in sorted(pairs, key=lambda p: ((p[1].medium or 1), (p[1].index or p[1].medium_index or 0))):
        track_dist = dist.tracks.get(info)
        length_diff = (item.length or 0) - (info.length or 0) if item.length and info.length else None
        tracks.append({
            "file": os.path.basename(os.fsdecode(item.path)),
            "file_title": item.title or "",
            "file_length": round(item.length, 1) if item.length else None,
            "index": info.index or info.medium_index,
            "medium": info.medium,
            "title": info.title or "",
            "length": round(info.length, 1) if info.length else None,
            "length_diff": round(length_diff, 1) if length_diff is not None else None,
            "distance": round(track_dist.distance, 3) if track_dist is not None else None,
            "penalties": _penalties(track_dist) if track_dist is not None else [],
        })
    return {
        "release": album_info.album_id,
        "album": f"{album_info.artist} - {album_info.album}",
        "similarity": round(1 - dist.distance, 3),
        "threshold": round(1 - threshold, 3),
        "penalties": _penalties(dist),
        "tracks": tracks,
        "missing": [{"index": t.index or t.medium_index, "medium": t.medium, "title": t.title or "",
                     "length": round(t.length, 1) if t.length else None} for t in extra_tracks],
        "unmatched": [os.path.basename(os.fsdecode(i.path)) for i in extra_items],
    }


def main(argv: list[str]) -> int:
    beets_dir, overlay, release_mbid, album_dir = argv
    os.environ["BEETSDIR"] = beets_dir
    from beets import config, plugins
    config.set_file(overlay)
    plugins.load_plugins()  # the configured ones, musicbrainz among them (beets 2.14: no arguments)
    from beets import metadata_plugins
    from beets.library import Item

    files = _audio_files(Path(album_dir))
    if not files:
        print(json.dumps({"error": f"no audio files in {album_dir}"}))
        return 0
    items = [Item.from_path(str(p)) for p in files]
    infos = list(metadata_plugins.albums_for_ids([release_mbid]))
    if not infos:
        print(json.dumps({"error": f"beets found no release {release_mbid} (MusicBrainz down, or the ID is wrong)"}))
        return 0
    print(json.dumps(explain(items, infos[0], config["match"]["strong_rec_thresh"].as_number())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
