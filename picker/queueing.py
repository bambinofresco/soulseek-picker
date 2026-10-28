"""The album queue: albums the picker searches, picks and downloads on its own (spec section 6).

A `queueitem` is one album to obtain, with a mode (full, tracks, upgrade) and a source (a
playlist's run, the album page, search results, the Stuck list). One `queue` job ticks every
few seconds while any item is active and moves items along:

    queued → searching → searched → downloading → importing → done

with side exits: `needs_pick` (the best folder is matched too weakly, or could be several
editions the preferences rank differently, or the release is Various Artists), `none_found`
(no FLAC folder; searched again after queue_retry_days, the longer search the second time, up
to three times), `review` (the import waits on the user, as today), `failed` (every folder
tried failed), `skipped`, `cancelled`.

Rules the loop keeps: one search at a time with a gap between them (the Soulseek server
throttles clients that flood it), at most queue_concurrency downloads, the recommendation
card's pick and only when its tier reaches auto_pick_min_tier, at most queue_candidates
folders per album. A download the user starts by hand on the album page satisfies the item.
Only this module changes item states; the scheduler just makes sure the job is running.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import col, select

from . import albums, plexsync, prefetching, recommend
from .clients.http import ServiceError
from .config import Settings, get_settings
from .db import session
from .downloading import start_download
from .editions import Edition
from .jobs import handler, log_event, worker
from .matching import TIER_RANK
from .models import Album, Download, Import, Job, Playlist, PlaylistAlbum, PlaylistTrack, QueueItem, Search, utcnow
from .preferences import score_edition
from .searching import latest_search, next_best_candidate, ordered_candidates, search_more, start_search

log = logging.getLogger(__name__)

ACTIVE = ("queued", "searching", "searched", "downloading", "importing")
WAITING = ("needs_pick", "none_found", "review")
TERMINAL = ("done", "failed", "skipped", "cancelled")
ACTIVE_SEARCH = ("queued", "searching")
ACTIVE_DOWNLOAD = ("queued", "enqueued", "transferring")
IMPORT_WAITING = ("review", "skipped", "blocked")
SEARCH_GAP = timedelta(seconds=5)
SEARCH_RETRY = timedelta(minutes=10)  # after slskd failed the search itself
REUSE_SEARCH = timedelta(minutes=10)  # a finished search this young is used as is
MAX_SEARCHES = 3
TICK_SECONDS = 3.0
SCHEDULER_SECONDS = 60.0

_last_search_end: datetime | None = None


def _save(item: QueueItem, **fields) -> QueueItem:
    for name, value in fields.items():
        setattr(item, name, value)
    item.updated_at = utcnow()
    if item.state in TERMINAL and item.finished_at is None:
        item.finished_at = item.updated_at
    with session() as s:
        s.merge(item)
        s.commit()
    return item


def _event(item: QueueItem, message: str) -> None:
    log_event(None, item.rg_mbid, f"Queue: {message}")


# Adding and controlling ---------------------------------------------------------------------

def active_item(rg_mbid: str) -> QueueItem | None:
    with session() as s:
        return s.exec(select(QueueItem).where(QueueItem.rg_mbid == rg_mbid, col(QueueItem.state).in_(ACTIVE + WAITING))
                      .order_by(QueueItem.id.desc())).first()


def add(rg_mbid: str, mode: str = "full", *, source: str = "album", playlist_id: int | None = None,
        wanted: list[str] | None = None, owned: dict | None = None) -> QueueItem:
    """Queue an album; an item for it that is still open is reused."""
    if mode not in ("full", "tracks", "upgrade"):
        raise ValueError(f"unknown mode {mode!r}")
    existing = active_item(rg_mbid)
    if existing:
        if playlist_id and not existing.playlist_id:
            _save(existing, playlist_id=playlist_id)
        return existing
    item = QueueItem(rg_mbid=rg_mbid, mode=mode, source=source, playlist_id=playlist_id, wanted=wanted or [], owned=owned)
    with session() as s:
        s.add(item)
        s.commit()
        s.refresh(item)
    ensure_running()
    return item


def skip(item_id: int) -> None:
    with session() as s:
        item = s.get(QueueItem, item_id)
    if item and item.state not in TERMINAL:
        _save(item, state="skipped", note="skipped by the user")


def requeue(item_id: int) -> None:
    """Try again: a failed, skipped, cancelled or none_found item goes back to queued."""
    with session() as s:
        item = s.get(QueueItem, item_id)
    if item and item.state in TERMINAL + ("none_found", "needs_pick"):
        _save(item, state="queued", next_try=None, finished_at=None, note="queued again by the user")
        ensure_running()


async def start_run(playlist_id: int, settings: Settings | None = None) -> int:
    """One click authorizes the run: every album the review page does not skip becomes a queue item."""
    from .playlists import review  # imported here: playlists is a user of this module too

    settings = settings or get_settings()
    data = await review(playlist_id, settings)
    if data is None:
        return 0
    created = 0
    for group in data.groups:
        if group.mode == "skip":
            continue
        wanted = [t.recording_mbid for t in group.tracks if not t.owned_path and t.recording_mbid]
        item = add(group.rg_mbid, group.mode, source="playlist", playlist_id=playlist_id, wanted=wanted,
                   owned=group.owned.to_dict() if group.owned else None)
        created += 1
        with session() as s:
            choice = s.get(PlaylistAlbum, group.choice.id)
            choice.queue_item_id = item.id
            s.add(choice)
            for track in group.tracks:
                row = s.get(PlaylistTrack, track.id)
                row.queue_item_id = item.id
                s.add(row)
            s.commit()
    with session() as s:
        playlist = s.get(Playlist, playlist_id)
        playlist.state = "running"
        s.add(playlist)
        s.commit()
    log_event(None, None, f"Run started for playlist “{data.playlist.name}”: {created} album(s) queued")
    ensure_running()
    plexsync.queue_sync(playlist_id, timedelta(0))  # the tracks already owned go into the playlist now
    return created


def set_playlist_state(playlist_id: int, state: str) -> None:
    """running | paused | cancelled. Cancelling closes the items not yet downloading; running ones finish."""
    with session() as s:
        playlist = s.get(Playlist, playlist_id)
        if playlist is None:
            return
        if state == "cancelled":
            for item in s.exec(select(QueueItem).where(QueueItem.playlist_id == playlist_id,
                                                        col(QueueItem.state).in_(("queued", "searching", "searched", "needs_pick", "none_found")))):
                item.state, item.note, item.finished_at, item.updated_at = "cancelled", "run cancelled", utcnow(), utcnow()
                s.add(item)
            playlist.state = "done"
        else:
            playlist.state = state
        s.add(playlist)
        s.commit()
    if state == "running":
        ensure_running()


# Scheduling -----------------------------------------------------------------------------------

def ensure_running() -> Job | None:
    with session() as s:
        pending = s.exec(select(Job).where(Job.kind == "queue", col(Job.status).in_(["queued", "running"]))).first()
    return None if pending else worker.enqueue("queue", {})


def needs_ticking() -> bool:
    """Anything the loop should look at: active items, due retries, or a waiting item the user acted on."""
    now = utcnow()
    with session() as s:
        items = s.exec(select(QueueItem).where(col(QueueItem.state).in_(ACTIVE + WAITING))).all()
        for item in items:
            if item.state in ACTIVE:
                return True
            if item.state == "none_found" and item.next_try and item.next_try <= now:
                return True
            if item.state in ("needs_pick", "review") and _newer_download(s, item) is not None:
                return True
            if item.state == "review" and item.import_id:
                imp = s.get(Import, item.import_id)
                if imp and imp.state not in IMPORT_WAITING:
                    return True
    return False


async def scheduler() -> None:
    """Keep the queue job alive while there is work, including retries due days later."""
    while True:
        try:
            if needs_ticking():
                ensure_running()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("queue scheduler")
        await asyncio.sleep(SCHEDULER_SECONDS)


@handler("queue")
async def run_queue(job: Job) -> None:
    while True:
        settings = get_settings()
        await tick(settings)
        with session() as s:
            active = s.exec(select(QueueItem.id).where(col(QueueItem.state).in_(ACTIVE))).first()
        if active is None:
            return
        await asyncio.sleep(TICK_SECONDS)


# The tick ---------------------------------------------------------------------------------------

def _newer_download(s, item: QueueItem) -> Download | None:
    """A download of this album started after the item (by hand, or by an earlier tick we lost track of)."""
    query = select(Download).where(Download.rg_mbid == item.rg_mbid, Download.created_at >= item.created_at,
                                   Download.refetch_import_id.is_(None))
    if item.download_id:
        query = query.where(Download.id > item.download_id)
    return s.exec(query.order_by(Download.id.desc())).first()


async def tick(settings: Settings) -> None:
    now = utcnow()
    with session() as s:
        items = s.exec(select(QueueItem).where(col(QueueItem.state).in_(ACTIVE + WAITING)).order_by(QueueItem.id)).all()
        paused = {p.id for p in s.exec(select(Playlist).where(Playlist.state == "paused"))}
        for item in items:
            if item.state in ("queued", "searched", "needs_pick", "review"):
                dl = _newer_download(s, item)
                if dl is not None:
                    _adopt(item, dl)
    for item in items:
        if item.state == "none_found" and item.next_try and item.next_try <= now:
            _save(item, state="queued", next_try=None, note="searching again")
    for item in items:
        if item.state == "searching":
            _check_search(item)
    for item in items:
        if item.state == "downloading":
            _check_download(item, settings)
    for item in items:
        if item.state in ("importing", "review"):
            _check_import(item, settings)

    downloading = sum(1 for i in items if i.state == "downloading")
    for item in items:
        if item.state == "searched" and item.playlist_id not in paused:
            if downloading >= settings.queue_concurrency:
                break
            if await _decide(item, settings):
                downloading += 1

    if not any(i.state == "searching" for i in items):
        gap_over = _last_search_end is None or utcnow() - _last_search_end >= SEARCH_GAP
        if gap_over:
            for item in items:
                if item.state == "queued" and item.playlist_id not in paused and (item.next_try is None or item.next_try <= now):
                    _start_search(item)
                    break
    _finish_playlists()


def _adopt(item: QueueItem, dl: Download) -> None:
    with session() as s:
        imp = s.exec(select(Import).where(Import.download_id == dl.id)).first()
    fields = dict(download_id=dl.id, search_id=dl.search_id, next_try=None,
                  candidates_tried=sorted(set(item.candidates_tried or []) | {dl.candidate_id}))
    if imp is not None:
        _save(item, import_id=imp.id, state="importing", note=f"import of {dl.username}'s folder", **fields)
    else:
        _save(item, state="downloading", note=f"downloading {dl.username}'s folder (started by hand)", **fields)


def _start_search(item: QueueItem) -> None:
    global _last_search_end
    latest = latest_search(item.rg_mbid)
    if latest and latest.state in ACTIVE_SEARCH:
        _save(item, search_id=latest.id, state="searching", note="a search is already running")
        return
    if latest and latest.state == "done" and item.attempts == 0 and latest.finished_at \
            and utcnow() - latest.finished_at < REUSE_SEARCH:
        _save(item, search_id=latest.id, state="searched", note="using the search from a moment ago")
        return
    if latest and latest.state == "done" and item.attempts >= 1:
        search = search_more(latest)
        _save(item, search_id=search.id, state="searching", note="longer search")
        _event(item, "longer search (nothing found last time)")
        return
    search = start_search(item.rg_mbid)
    _save(item, search_id=search.id, state="searching", note="searching Soulseek")


def _check_search(item: QueueItem) -> None:
    global _last_search_end
    with session() as s:
        search = s.get(Search, item.search_id) if item.search_id else None
    if search is None:
        _save(item, state="queued", search_id=None)
        return
    if search.state in ACTIVE_SEARCH:
        return
    _last_search_end = search.finished_at or utcnow()
    if search.state == "failed":
        tries = item.attempts + 1
        if tries >= MAX_SEARCHES:
            _save(item, state="failed", attempts=tries, note=f"the search failed {tries} times: {search.error}")
        else:
            _save(item, state="queued", attempts=tries, next_try=utcnow() + SEARCH_RETRY,
                  note=f"the search failed ({search.error}); trying again in {int(SEARCH_RETRY.total_seconds() // 60)} min")
        return
    if prefetching.active_prefetches(search.id):
        return  # rip logs still coming in; the match may still improve
    _save(item, state="searched", note="search finished")


async def _editions_for(rg_mbid: str) -> dict[str, Edition] | None:
    try:
        return {e.id: e for e in await albums.album_editions(rg_mbid)}
    except ServiceError as exc:
        log.warning("queue: editions for %s: %s", rg_mbid, exc.message)
        return None


def _none_found(item: QueueItem, settings: Settings, reason: str) -> None:
    tries = item.attempts + 1
    if tries >= MAX_SEARCHES:
        _save(item, state="failed", attempts=tries, note=f"{reason} ({tries} searches)")
        _event(item, f"given up: {reason}")
        return
    retry = utcnow() + timedelta(days=settings.queue_retry_days)
    _save(item, state="none_found", attempts=tries, next_try=retry,
          note=f"{reason}; searching again in {settings.queue_retry_days} day(s)")
    _event(item, f"{reason}; will search again on {retry.date().isoformat()}")


def _needs_pick(item: QueueItem, note: str) -> None:
    _save(item, state="needs_pick", note=note)
    _event(item, f"needs a pick: {note}")


async def _decide(item: QueueItem, settings: Settings) -> bool:
    """Pick the recommended folder and start it; True when a download started."""
    with session() as s:
        search = s.get(Search, item.search_id) if item.search_id else None
        album = s.get(Album, item.rg_mbid)
    if search is None:
        _save(item, state="queued", search_id=None)
        return False
    if album and album.artist.strip().lower() == "various artists":
        _needs_pick(item, "a Various Artists release is never downloaded unasked")
        return False
    if item.mode == "tracks":
        _needs_pick(item, "tracks-only downloads are picked by hand for now")
        return False
    editions = await _editions_for(item.rg_mbid)
    if editions is None:
        _save(item, note="MusicBrainz did not answer for the editions; trying again shortly")
        return False
    candidates, _ = ordered_candidates(search, editions=editions)
    tried = set(item.candidates_tried or [])
    candidates = [c for c in candidates if c.id not in tried]
    rec = recommend.recommend(candidates, editions, settings)
    if rec is None:
        _none_found(item, settings, "no FLAC folder found" if not candidates else "no folder matched an edition")
        return False
    best = rec.best
    want = TIER_RANK.get(settings.auto_pick_min_tier, TIER_RANK["High"])
    summary = "; ".join(best.summary[:3])
    if TIER_RANK.get(best.tier, 0) < want:
        _needs_pick(item, f"best folder is {best.tier} ({best.candidate.username}: {summary}); the queue downloads {settings.auto_pick_min_tier} or better")
        return False
    matched = [editions[r] for r in (best.candidate.match or {}).get("editions") or [] if r in editions]
    if not matched or best.edition is None:
        _needs_pick(item, f"{best.candidate.username}'s folder matched no edition that is still in MusicBrainz")
        return False
    if len({score_edition(e, settings).score for e in matched}) > 1:
        _needs_pick(item, f"{best.candidate.username}'s folder could be one of {len(matched)} editions the preferences rank differently")
        return False
    upgrade_of = (item.owned or {}).get("path") if item.mode == "upgrade" else None
    dl = start_download(best.candidate, item.rg_mbid, best.edition.id, upgrade_of=upgrade_of)
    _save(item, state="downloading", download_id=dl.id, candidates_tried=sorted(tried | {best.candidate.id}),
          note=f"downloading {best.candidate.username}'s folder ({best.tier}) as the {best.edition.date or ''} {best.edition.country or ''} edition".replace("  ", " "))
    _event(item, f"picked {best.candidate.username}'s folder: {summary}")
    return True


def _next_candidate(item: QueueItem, dl: Download, settings: Settings, why: str) -> None:
    tried = set(item.candidates_tried or []) | {dl.candidate_id}
    if len(tried) >= settings.queue_candidates:
        _save(item, state="failed", candidates_tried=sorted(tried), note=f"{why}; {len(tried)} folders tried")
        _event(item, f"given up after {len(tried)} folders: {why}")
        return
    candidate = next_best_candidate(dl.search_id, tried, dl.release_mbid)
    if candidate is None:
        _save(item, state="failed", candidates_tried=sorted(tried), note=f"{why}; no other folder could be this edition")
        _event(item, f"{why}; no other folder could be this edition")
        return
    new = start_download(candidate, item.rg_mbid, dl.release_mbid, upgrade_of=dl.upgrade_of)
    _save(item, state="downloading", download_id=new.id, import_id=None, candidates_tried=sorted(tried),
          note=f"{why}; trying {candidate.username}'s folder next")
    _event(item, f"{why}; trying {candidate.username}'s folder next")


def _check_download(item: QueueItem, settings: Settings) -> None:
    with session() as s:
        dl = s.get(Download, item.download_id) if item.download_id else None
        imp = s.exec(select(Import).where(Import.download_id == dl.id)).first() if dl else None
    if dl is None:
        _save(item, state="searched", download_id=None)
        return
    if imp is not None:
        _save(item, import_id=imp.id, state="importing", note="downloaded; checking the files")
        return
    if dl.state in ACTIVE_DOWNLOAD or dl.state == "completed":
        return  # completed: the import row appears a moment later
    _next_candidate(item, dl, settings, f"{dl.username}'s download {dl.state}" + (f" ({dl.error})" if dl.error else ""))


def _check_import(item: QueueItem, settings: Settings) -> None:
    with session() as s:
        imp = s.get(Import, item.import_id) if item.import_id else None
        dl = s.get(Download, item.download_id) if item.download_id else None
    if imp is None:
        _save(item, state="downloading" if dl else "searched", import_id=None)
        return
    if imp.state == "imported":
        _save(item, state="done", note=f"imported into {imp.album_dir}")
        _event(item, f"done: {imp.album_dir}")
        if item.playlist_id:
            plexsync.queue_sync(item.playlist_id)  # the playlist gains this album's tracks
    elif imp.state in IMPORT_WAITING:
        if item.state != "review":
            _save(item, state="review", note=imp.error or f"waiting at review ({imp.step})")
    elif imp.state in ("rejected", "failed"):
        if dl is not None:
            _next_candidate(item, dl, settings, "rejected" if imp.state == "rejected" else f"import failed ({imp.error})")
        else:
            _save(item, state="failed", note=imp.error or imp.state)
    elif item.state == "review":
        _save(item, state="importing", note="importing")


def _finish_playlists() -> None:
    with session() as s:
        for playlist in s.exec(select(Playlist).where(col(Playlist.state).in_(("running", "paused")))):
            open_items = s.exec(select(QueueItem.id).where(QueueItem.playlist_id == playlist.id,
                                                             col(QueueItem.state).in_(ACTIVE + WAITING))).first()
            if open_items is None:
                playlist.state = "done"
                s.add(playlist)
        s.commit()


# For other modules and the pages ------------------------------------------------------------------

def item_for_download(download_id: int) -> QueueItem | None:
    with session() as s:
        return s.exec(select(QueueItem).where(QueueItem.download_id == download_id)).first()


@dataclass
class RunSummary:
    counts: dict = field(default_factory=dict)
    items: list[QueueItem] = field(default_factory=list)
    albums: dict = field(default_factory=dict)
    imports: dict = field(default_factory=dict)

    def where(self, *states: str) -> list[QueueItem]:
        return [i for i in self.items if i.state in states]

    @property
    def open(self) -> int:
        return sum(1 for i in self.items if i.state in ACTIVE + WAITING)


def summary(playlist_id: int | None = None, limit: int | None = None) -> RunSummary:
    with session() as s:
        query = select(QueueItem).order_by(QueueItem.id.desc())
        if playlist_id is not None:
            query = query.where(QueueItem.playlist_id == playlist_id)
        if limit:
            query = query.limit(limit)
        items = s.exec(query).all()
        rgs = {i.rg_mbid for i in items}
        album_rows = {a.rg_mbid: a for a in s.exec(select(Album).where(col(Album.rg_mbid).in_(rgs)))} if rgs else {}
        import_ids = [i.import_id for i in items if i.import_id]
        imports = {i.id: i for i in s.exec(select(Import).where(col(Import.id).in_(import_ids)))} if import_ids else {}
    counts: dict[str, int] = {}
    for item in items:
        counts[item.state] = counts.get(item.state, 0) + 1
    return RunSummary(counts=counts, items=items, albums=album_rows, imports=imports)
