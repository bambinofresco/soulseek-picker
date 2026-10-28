"""The search job: query slskd, group responses into candidates, store them."""

from __future__ import annotations

import asyncio
import time

from sqlmodel import delete, select

from . import albums
from .clients import services
from .clients.http import ServiceError
from .config import Settings, get_settings
from . import preferences
from .db import session
from .grouping import (
    CandidateDraft,
    availability_key,
    candidate_drafts,
    group_responses,
    passes_default_filter,
)
from .jobs import handler, log_event, worker
from .models import Album, Candidate, Job, Search, utcnow
from .editions import Edition, editions_from_releases
from .matching import TIER_RANK, match_candidate
from .parsing import parse_candidate
from .prefetching import auto_prefetch, cue_barcodes, disc_id_releases
from .queries import fallback_queries, primary_query

POLL_INTERVAL = 1.0
DEEP_MAX_SECONDS = 180  # "Search more": let responses trickle in for up to this long
RESULTS_WAIT = 10.0  # slskd marks a search complete a moment before it saves the responses


def start_search(rg_mbid: str) -> Search:
    with session() as s:
        search = Search(rg_mbid=rg_mbid)
        s.add(search)
        s.commit()
        s.refresh(search)
    job = worker.enqueue("search", {"search_id": search.id}, rg_mbid=rg_mbid)
    with session() as s:
        search.job_id = job.id
        s.merge(search)
        s.commit()
    return search


def search_more(search: Search) -> Search:
    """A longer search for the same album, merged into the same table."""
    _update(search, state="queued", deep=True, error=None)
    job = worker.enqueue("search", {"search_id": search.id, "deep": True}, rg_mbid=search.rg_mbid)
    _update(search, job_id=job.id)
    return search


def _update(search: Search, **fields) -> None:
    for name, value in fields.items():
        setattr(search, name, value)
    with session() as s:
        s.merge(search)
        s.commit()


async def _run_query(search: Search, text: str, base_counts: tuple[int, int], deep: bool) -> tuple[list[dict], bool]:
    """Run one slskd search; (responses, stopped early).

    slskd only hands out responses once a search has ended, and a search keeps going while
    responses trickle in. A normal search is therefore stopped once it has enough to show
    (search_quick_responses after search_quick_seconds) or at search_max_seconds; "Search
    more" lets it run much longer.
    """
    settings = get_settings()
    slskd = services().slskd
    search_id = await slskd.start_search(
        text,
        timeout_s=settings.search_timeout,
        response_limit=settings.search_response_limit,
        file_limit=settings.search_file_limit,
    )
    started = time.monotonic()
    max_seconds = DEEP_MAX_SECONDS if deep else settings.search_max_seconds
    stopped = False
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            state = await slskd.get_search(search_id)
            responses = int(state.get("responseCount") or 0)
            _update(search, response_count=base_counts[0] + responses,
                    file_count=base_counts[1] + int(state.get("fileCount") or 0))
            if state.get("isComplete"):
                break
            elapsed = time.monotonic() - started
            enough = (not deep and elapsed >= settings.search_quick_seconds
                      and responses >= settings.search_quick_responses)
            if not stopped and (enough or elapsed >= max_seconds):
                await slskd.stop_search(search_id)
                stopped = True
        # slskd saves the responses (and sets endedAt) just after it reports the search complete.
        deadline = time.monotonic() + RESULTS_WAIT
        while not state.get("endedAt") and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            state = await slskd.get_search(search_id)
        return await slskd.search_responses(search_id), stopped
    except asyncio.CancelledError:
        await slskd.stop_search(search_id)
        raise


def _flac_folder_count(drafts: list[CandidateDraft], min_tracks: int | None) -> int:
    count = 0
    for d in drafts:
        summary = d.summary()
        if passes_default_filter(summary["audio_format"], summary["audio_count"], min_tracks, flac_only=True):
            count += 1
    return count


@handler("search")
async def run_search(job: Job) -> None:
    deep = bool(job.payload.get("deep"))
    with session() as s:
        search = s.get(Search, job.payload["search_id"])
        if not deep:
            # A restart re-runs the whole search; clear anything a previous attempt stored.
            s.exec(delete(Candidate).where(Candidate.search_id == search.id))
            s.commit()
        album = s.get(Album, search.rg_mbid)
    try:
        await _search(job, search, album, deep)
    except Exception as exc:
        _update(search, state="failed", current_query=None, error=str(exc), finished_at=utcnow())
        raise


async def _load_editions(album: Album) -> tuple[int | None, list[str], list[Edition], str | None]:
    try:
        min_tracks = await albums.load_min_track_count(album)
        releases = await albums.editions(album.rg_mbid)
        return min_tracks, albums.label_names(releases), editions_from_releases(releases), None
    except ServiceError as exc:
        return None, [], [], exc.message


async def _search(job: Job, search: Search, album: Album, deep: bool = False) -> None:
    settings = get_settings()
    # MusicBrainz (1 request/s) loads the editions while slskd searches.
    editions_task = asyncio.create_task(_load_editions(album))

    queries = [primary_query(album.artist, album.title)]
    if deep:
        _update(search, state="searching", current_query=None, error=None)
    else:
        _update(search, state="searching", queries=queries, response_count=0, file_count=0, error=None,
                stopped_early=False, deep=False)

    drafts: dict = {}
    pending = list(queries)
    fallbacks = [] if deep else fallback_queries(album.artist, album.title)
    stopped_early = False
    while pending:
        text = pending.pop(0)
        _update(search, current_query=text)
        log_event(job.id, album.rg_mbid, f"Searching Soulseek for “{text}”" + (" (longer search)" if deep else ""))
        responses, stopped = await _run_query(search, text, (search.response_count, search.file_count), deep)
        stopped_early |= stopped
        group_responses(responses, text, drafts)
        min_tracks, *_ = await editions_task
        found = _flac_folder_count(candidate_drafts(drafts), min_tracks)
        log_event(job.id, album.rg_mbid, f"“{text}”: {len(responses)} users responded; {found} FLAC folders so far"
                                         + (" (stopped early to show results sooner)" if stopped else ""))
        if not pending and fallbacks and found < settings.min_flac_folders:
            pending, fallbacks = fallbacks, []
            queries += pending
            _update(search, queries=queries)
            log_event(job.id, album.rg_mbid, f"Fewer than {settings.min_flac_folders} FLAC folders; trying fallback queries")

    min_tracks, labels, editions, error = await editions_task
    if error:
        log_event(job.id, album.rg_mbid, f"Could not load editions ({error}); filtering, label hints and matching are off")
    added, updated = _store(search, drafts, album, labels, editions)
    _update(search, state="done", current_query=None, finished_at=utcnow(),
            stopped_early=False if deep else stopped_early, deep=False)
    log_event(job.id, album.rg_mbid, f"Search finished: {added} new folder(s)" + (f", {updated} updated" if updated else ""))
    if editions:
        started = auto_prefetch(search, ordered_candidates(search)[0])
        if started:
            log_event(job.id, album.rg_mbid, f"Fetching rip logs from {started} folder(s) with a free upload slot")


def _store(search: Search, drafts: dict, album: Album, labels: list[str], editions: list[Edition]) -> tuple[int, int]:
    """Insert new folders and merge new files into folders already in the table.

    Existing rows keep their log results and downloads; their match uses the disc IDs and
    cue barcodes their log prefetch already found.
    """
    with session() as s:
        existing = {(c.username, c.folder): c for c in s.exec(select(Candidate).where(Candidate.search_id == search.id))}
    added = updated = 0
    rows = []
    for draft in candidate_drafts(drafts):
        old = existing.get((draft.username, draft.folder))
        if old:
            known = {f["filename"]: f for f in old.files or []}
            if all(name in known for name in draft.files):
                continue  # nothing new in this folder
            draft.files = {**known, **draft.files}
        summary = draft.summary()
        parsed = parse_candidate(
            [f["filename"] for f in summary["files"]],
            files=summary["files"], artist=album.artist, album=album.title, labels=labels,
        ).to_dict()
        info = (old.log_info if old else None) or {}
        mine, _ = disc_id_releases(info, album.rg_mbid)
        match = match_candidate(summary["files"], parsed, editions, disc_id_releases=mine,
                                barcodes_extra=cue_barcodes(info)).to_dict()
        fields = dict(has_free_slot=draft.has_free_slot, queue_length=draft.queue_length,
                      upload_speed=draft.upload_speed, parsed=parsed, match=match, **summary)
        if old:
            with session() as s:
                row = s.get(Candidate, old.id)
                for name, value in fields.items():
                    setattr(row, name, value)
                s.add(row)
                s.commit()
            updated += 1
        else:
            rows.append(Candidate(search_id=search.id, username=draft.username, folder=draft.folder,
                                  query=draft.query, **fields))
            added += 1
    with session() as s:
        s.add_all(rows)
        s.commit()
    return added, updated


def latest_search(rg_mbid: str) -> Search | None:
    with session() as s:
        return s.exec(select(Search).where(Search.rg_mbid == rg_mbid).order_by(Search.id.desc())).first()


def parsed(c: Candidate) -> dict:
    return c.parsed or {}


def quality(c: Candidate) -> tuple[int | None, int | None]:
    """Bit depth and sample rate: slskd attributes, else what the path says."""
    p = parsed(c)
    return c.bit_depth or p.get("bit_depth"), c.sample_rate or p.get("sample_rate")


def _availability(c: Candidate) -> tuple:
    return availability_key(c.has_free_slot, c.queue_length, c.upload_speed)


def match_of(c: Candidate) -> dict:
    return c.match or {}


def _match_rank(c: Candidate) -> tuple:
    m = match_of(c)
    return (-TIER_RANK.get(m.get("tier") or "Unknown", 0), -(m.get("score") or 0))


def pref_score(c: Candidate, editions: dict[str, Edition] | None, settings: Settings) -> preferences.PrefScore:
    """The edition-preference score of a folder (what its name says plus the editions it could be)."""
    matched = [editions[r] for r in match_of(c).get("editions") or [] if editions and r in editions]
    return preferences.score_candidate(parsed(c), quality(c)[0], matched, settings)


def default_key(c: Candidate, settings: Settings, editions: dict[str, Edition] | None = None) -> tuple:
    """Tier, then the edition preferences, then prefer_original, then availability.

    prefer_original puts earlier editions first within a tier and remaster-flagged
    folders below unflagged ones; the preference score (remasters, countries, sources,
    depth, flags) comes before it.
    """
    tier = -TIER_RANK.get(match_of(c).get("tier") or "Unknown", 0)
    pref: tuple = (-pref_score(c, editions, settings).score,)
    if settings.prefer_original:
        pref += (parsed(c).get("remaster") == "folder", match_of(c).get("earliest_date") or "9999")
    return (tier, *pref, *_availability(c))


SORT_KEYS = {
    "match": _match_rank,
    "availability": _availability,
    "source": lambda c: parsed(c).get("source") or "~",
    "format": lambda c: tuple(-(v or 0) for v in quality(c)),
    "tracks": lambda c: -c.audio_count,
    "years": lambda c: parsed(c).get("original_year") or parsed(c).get("reissue_year") or 9999,
    "catalog": lambda c: -len(parsed(c).get("catalog_numbers") or []),
    "flags": lambda c: -len(parsed(c).get("flags") or []),
    "sidecars": lambda c: (-c.sidecars.get("log", 0), -c.sidecars.get("cue", 0)),
    "size": lambda c: -c.total_size,
}


def ordered_candidates(
    search: Search,
    show_all: bool = False,
    *,
    source: str | None = None,
    depth: int | None = None,
    sort: str | None = None,
    reverse: bool = False,
    target: str | None = None,
    editions: dict[str, Edition] | None = None,
) -> tuple[list[Candidate], int]:
    """Candidates in display order, and how many the default filter hid.

    The default order is tier, preference, availability; a column sort breaks ties
    with that order. `target` keeps only folders whose ambiguity set includes that edition.
    `editions` (id -> Edition) lets the preferences see countries and formats; without it
    only what the folder name says counts.
    """
    settings = get_settings()
    with session() as s:
        album = s.get(Album, search.rg_mbid)
        rows = list(s.exec(select(Candidate).where(Candidate.search_id == search.id)))
    min_tracks = album.min_track_count if album else None
    visible = [
        c for c in rows
        if show_all or passes_default_filter(c.audio_format, c.audio_count, min_tracks, settings.flac_only)
    ]
    hidden = len(rows) - len(visible)
    if source:
        visible = [c for c in visible if (parsed(c).get("source") or "unknown") == source]
    if depth:
        visible = [c for c in visible if quality(c)[0] == depth]
    if target:
        visible = [c for c in visible if target in (match_of(c).get("editions") or [])]
    visible.sort(key=lambda c: default_key(c, settings, editions))
    if sort in SORT_KEYS:
        visible.sort(key=SORT_KEYS[sort], reverse=reverse)
    return visible, hidden


def next_best_candidate(search_id: int, tried: set[int], release_mbid: str | None = None) -> Candidate | None:
    """The best untried folder that could be the same edition (any folder if none given)."""
    with session() as s:
        search = s.get(Search, search_id)
    for candidate in ordered_candidates(search, target=release_mbid)[0]:
        if candidate.id not in tried:
            return candidate
    return None
