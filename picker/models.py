from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def json_field(default_factory=dict) -> Any:
    return Field(default_factory=default_factory, sa_column=Column(JSON))


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    kind: str  # search | download
    status: str = "queued"  # queued | running | done | failed
    payload: dict = json_field()
    rg_mbid: str | None = Field(default=None, index=True)
    error: str | None = None
    run_after: datetime | None = None  # not started before this time (debounced jobs)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class JobEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int | None = Field(default=None, index=True)
    rg_mbid: str | None = Field(default=None, index=True)
    ts: datetime = Field(default_factory=utcnow)
    message: str


class Album(SQLModel, table=True):
    rg_mbid: str = Field(primary_key=True)
    artist_mbid: str | None = None
    artist: str
    title: str
    first_release_date: str | None = None
    primary_type: str | None = None
    min_track_count: int | None = None  # smallest edition, for the default filter
    target_release: str | None = None  # pinned edition: the table shows only folders that could be it
    created_at: datetime = Field(default_factory=utcnow)


class Search(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rg_mbid: str = Field(index=True)
    job_id: int | None = None
    state: str = "queued"  # queued | searching | done | failed
    queries: list = json_field(list)
    current_query: str | None = None
    response_count: int = 0
    file_count: int = 0
    error: str | None = None
    stopped_early: bool | None = None  # stopped once it had enough to show (searching.py)
    deep: bool | None = None  # a "Search more" pass is running
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class Candidate(SQLModel, table=True):
    """One user's folder; multi-disc subfolders are merged into their parent."""

    id: int | None = Field(default=None, primary_key=True)
    search_id: int = Field(index=True)
    username: str
    folder: str  # remote path, backslash-separated
    query: str  # the query that first found it
    files: list = json_field(list)  # see grouping.CandidateFile
    audio_count: int = 0
    disc_counts: list = json_field(list)
    total_size: int = 0
    audio_format: str = ""  # FLAC | MP3 | ... | mixed
    bit_depth: int | None = None
    sample_rate: int | None = None
    mixed_quality: bool = False
    sidecars: dict = json_field()  # {"log": n, "cue": n, "image": n, "scans": n, "text": n}
    has_free_slot: bool = False
    queue_length: int = 0
    upload_speed: int = 0
    parsed: dict | None = Field(default=None, sa_column=Column(JSON))  # parsing.ParsedFolder.to_dict()
    match: dict | None = Field(default=None, sa_column=Column(JSON))  # matching.MatchResult.to_dict()
    log_info: dict | None = Field(default=None, sa_column=Column(JSON))  # prefetching result


class Download(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(index=True)
    search_id: int = Field(index=True)
    rg_mbid: str = Field(index=True)
    job_id: int | None = None
    username: str
    folder: str
    release_mbid: str | None = None  # the edition the user confirmed
    destination: str  # relative to the slskd download directory
    batch_id: str | None = None
    legacy_enqueue: bool = False
    files: list = json_field(list)  # [{"filename", "size"}]
    state: str = "queued"  # queued | enqueued | transferring | completed | failed
    bytes_total: int = 0
    bytes_done: int = 0
    files_done: int = 0
    last_progress_at: datetime | None = None  # set once bytes flow; the stall clock
    queued_since: datetime | None = None  # set while waiting in the uploader's queue; the queue clock
    queue_position: int | None = None
    give_up: bool | None = None  # set by "Give up and try next"; the job stops on its next poll
    # filename -> {status, tries, error, not_before, failed_seen}; see picker/transfers.py
    file_states: dict | None = Field(default=None, sa_column=Column(JSON))
    window_files: int | None = None  # how much is requested from the uploader at once
    window_bytes: int | None = None
    chunked: str | None = None  # "planned" (big folder) | "limit" (uploader queue limit) | None
    refetch_import_id: int | None = None  # re-downloading files that failed this import's integrity check
    upgrade_of: str | None = None  # the library folder this download is meant to replace
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class Import(SQLModel, table=True):
    """One trip through the import pipeline; also the History row.

    Hand-picked imports start from a Download. Retags of Lidarr's own imports (phase 5)
    have no download and set `source` to "lidarr".
    """

    id: int | None = Field(default=None, primary_key=True)
    source: str = "picker"  # picker | lidarr
    download_id: int | None = Field(default=None, index=True)
    rg_mbid: str = Field(index=True)
    release_mbid: str | None = None
    username: str | None = None
    job_id: int | None = None
    # verify -> tags -> review -> artist -> place -> beets -> lidarr -> plex -> done
    step: str = "verify"
    # verifying | refetching | review | importing | imported | skipped | blocked | failed | rejected
    state: str = "verifying"
    refetches: int | None = None  # times files failing the integrity check were downloaded again
    source_dir: str | None = None  # where the files were before placing
    album_dir: str | None = None  # final folder in the library
    force: bool = False  # rerun beets with the strong_rec_thresh overlay
    integrity: list = json_field(list)  # per file: {"name", "ok", "detail"}
    spectrograms: list = json_field(list)  # file names under spectrogram_dir/<id>/
    transcode: dict | None = Field(default=None, sa_column=Column(JSON))  # audio.transcode_check
    tag_check: dict | None = Field(default=None, sa_column=Column(JSON))  # tagcheck.TagCheck: the uploader's tags vs the chosen edition
    rip_check: dict | None = Field(default=None, sa_column=Column(JSON))  # ripverify.verify_folder: CUETools DB / AccurateRip
    beets_output: str | None = None
    beets_match: dict | None = Field(default=None, sa_column=Column(JSON))  # why beets skipped (beets_explain)
    tags: dict | None = Field(default=None, sa_column=Column(JSON))  # audio.tag_snapshot after beets
    # Upgrades (spec 7.2): the folder being replaced, where it went, how old and new compare, whether it was put back.
    upgrade_of: str | None = None
    replaced_dir: str | None = None
    upgrade_check: dict | None = Field(default=None, sa_column=Column(JSON))  # importing.compare_upgrade
    upgrade_undone: bool | None = None
    notes: list = json_field(list)  # human-readable warnings for History
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class Playlist(SQLModel, table=True):
    """An uploaded playlist (picker/playlists.py): the CSV's rows, resolved to albums, and the run made from it."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    csv_hash: str | None = None
    state: str = "resolving"  # resolving | review | running | paused | done
    full_albums: bool = True  # the top toggle: full albums (True) or only the playlist's tracks
    prefer_studio_album: bool = True  # compilation tracks resolve to the earliest studio album holding them
    resolved: int = 0  # rows resolved so far, for the progress line
    error: str | None = None
    plex_rating_key: str | None = None
    m3u_path: str | None = None
    last_sync: datetime | None = None
    sync_note: str | None = None  # what the last sync did, or why it could not
    created_at: datetime = Field(default_factory=utcnow)


class PlaylistTrack(SQLModel, table=True):
    """One CSV row, and what MusicBrainz and the library say about it."""

    id: int | None = Field(default=None, primary_key=True)
    playlist_id: int = Field(index=True)
    position: int
    uri: str | None = None
    isrc: str | None = None
    title: str
    artist: str
    album_artist: str | None = None
    album: str
    duration_ms: int | None = None
    release_date: str | None = None
    recording_mbid: str | None = None
    recording_title: str | None = None
    recording_length: int | None = None  # ms
    rg_mbid: str | None = Field(default=None, index=True)  # the album chosen for this track
    single_rg_mbid: str | None = None  # the single or EP holding the recording, when one exists
    use_single: bool | None = None  # None: follow the playlist's top toggle
    options: list = json_field(list)  # every release group holding the recording: playlists.AlbumOption dicts
    resolution: str | None = None  # isrc | text-strong | text-weak | manual | none; None: not tried yet
    owned_path: str | None = None  # a library file that is this recording
    owned_how: str | None = None  # recording | name
    owned_unticked: bool = False  # the user said the by-name match is wrong
    dropped: bool = False
    queue_item_id: int | None = None
    plex_item_id: str | None = None
    note: str | None = None


class PlaylistAlbum(SQLModel, table=True):
    """The user's choice for one album of a playlist. Rows exist only for albums the page has shown."""

    id: int | None = Field(default=None, primary_key=True)
    playlist_id: int = Field(index=True)
    rg_mbid: str = Field(index=True)
    mode: str | None = None  # full | tracks | upgrade | skip; None follows the top toggle (or "skip" when owned)
    queue_item_id: int | None = None


class QueueItem(SQLModel, table=True):
    """One album the picker should obtain on its own (picker/queueing.py)."""

    id: int | None = Field(default=None, primary_key=True)
    rg_mbid: str = Field(index=True)
    mode: str = "full"  # full | tracks | upgrade
    wanted: list = json_field(list)  # recording MBIDs, for tracks mode
    owned: dict | None = Field(default=None, sa_column=Column(JSON))  # owned.Owned.to_dict(), for upgrades
    source: str = "album"  # playlist | album | search | stuck
    playlist_id: int | None = Field(default=None, index=True)
    # held | queued | searching | searched | downloading | importing | done
    # | needs_pick | none_found | review | failed | skipped | cancelled
    state: str = "queued"
    note: str | None = None
    search_id: int | None = None
    download_id: int | None = None
    import_id: int | None = None
    candidates_tried: list = json_field(list)
    attempts: int = 0  # searches that found nothing
    next_try: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class LibraryFile(SQLModel, table=True):
    """One audio file under the music directory, as the library index last saw it (picker/library.py)."""

    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)
    folder: str = Field(index=True)  # the file's directory: the album folder, or a disc folder under it
    size: int = 0
    mtime: float = 0.0
    duration: float | None = None  # seconds
    bit_depth: int | None = None
    sample_rate: int | None = None
    artist: str | None = None
    albumartist: str | None = None
    album: str | None = None
    title: str | None = None
    tracknumber: int | None = None
    discnumber: int | None = None
    release_mbid: str | None = Field(default=None, index=True)
    rg_mbid: str | None = Field(default=None, index=True)
    recording_mbid: str | None = Field(default=None, index=True)
    lidarr_file_id: int | None = None  # Lidarr's track file ID when Lidarr lists this path
    indexed_at: datetime = Field(default_factory=utcnow)


class CacheEntry(SQLModel, table=True):
    key: str = Field(primary_key=True)
    data: Any = Field(default=None, sa_column=Column(JSON))
    fetched_at: datetime = Field(default_factory=utcnow)
