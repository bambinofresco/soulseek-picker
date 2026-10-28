"""Pages and HTMX partials."""

from __future__ import annotations

import base64
import hashlib
import json
from html import escape
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlmodel import select

from . import albums, health, importing, lidarr_feed, owned, preferences, prefetching, probe
from .beets import config_warnings as beets_config_warnings
from .clients import reset_services, services
from .clients.http import ServiceError
from .clients.musicbrainz import parse_mb_url
from .config import SECRET_FIELDS, Settings, env_locked, get_settings, save_settings
from .db import session
from .downloading import downloads_for_search, request_give_up, retry_failed_files, start_download
from .models import Album, Candidate, Download, Import, Job, JobEvent, Search, utcnow
from .editions import Edition
from .naming import FolderValues, album_folder, unknown_tokens
from .matching import completeness, rip_quality
from .transfers import summary as transfer_summary
from .searching import (
    pref_score,
    latest_search,
    match_of,
    next_best_candidate,
    ordered_candidates,
    parsed,
    quality,
    search_more,
    start_search,
)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()

ACTIVE_SEARCH = {"queued", "searching"}
ACTIVE_DOWNLOAD = {"queued", "enqueued", "transferring"}
RESEARCH_AFTER = timedelta(minutes=10)  # Pick reuses a search younger than this


# Template filters

def _mb(size: int | None) -> str:
    return f"{(size or 0) / 1_000_000:,.0f} MB" if (size or 0) >= 10_000_000 else f"{(size or 0) / 1_000_000:.1f} MB"


def _khz(rate: int | None) -> str:
    if not rate:
        return "?"
    khz = rate / 1000
    return f"{khz:g}"


def _speed(bps: int | None) -> str:
    bps = bps or 0
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} MB/s"
    return f"{bps / 1000:.0f} kB/s"


def _duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _local(ts: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Stored times are UTC; show them in the container's time zone (TZ)."""
    return ts.astimezone().strftime(fmt) if ts else ""


def _edition_label(e: Edition | None) -> str:
    """'1997-06-16 · GB · CD · Parlophone · NODATA 02 · (remaster)'."""
    if e is None:
        return "an edition no longer in MusicBrainz"
    parts = [e.date or "no date", e.country, e.format_summary]
    parts += e.labels[:1] + e.catalog_numbers[:1]
    if e.disambiguation:
        parts.append(f"({e.disambiguation})")
    return " · ".join(p for p in parts if p)


def _ago(ts: datetime | None) -> str:
    """'3 min', '1 h 12 min' since a UTC timestamp."""
    if ts is None:
        return ""
    minutes = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _signed(seconds: float | None) -> str:
    return "" if seconds is None else f"{seconds:+.1f} s"


templates.env.filters.update(
    mb=_mb, khz=_khz, speed=_speed, duration=_duration, local=_local, edition=_edition_label, signed=_signed, ago=_ago,
)
templates.env.globals["app_warnings"] = lambda: health.path_warnings(get_settings()) + beets_config_warnings(get_settings())
templates.env.globals["waiting_count"] = lambda: len(importing.waiting_imports())


def render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context)


# Home and album search

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    with session() as s:
        jobs = s.exec(select(Job).where(Job.status.in_(["queued", "running"])).order_by(Job.id.desc())).all()
        recent = s.exec(select(Import).order_by(Import.id.desc()).limit(20)).all()
        titles = {a.rg_mbid: a for a in s.exec(select(Album))}
    return render(request, "home.html", jobs=jobs, recent=recent, albums=titles,
                  stuck=lidarr_feed.stuck_albums(), lidarr_configured=bool(get_settings().lidarr_api_key))


@router.get("/search", response_class=HTMLResponse)
async def album_search(request: Request, artist: str = "", album: str = ""):
    for text in (album, artist):
        parsed = parse_mb_url(text)
        if parsed:
            try:
                rg_mbid = await albums.resolve_mb_url(*parsed)
            except ServiceError as exc:
                return render(request, "search_results.html", artist=artist, album=album, results=[], error=exc.message)
            return RedirectResponse(f"/album/{rg_mbid}?search=1", status_code=303)
    results, error = [], None
    try:
        results = await services().mb.search_release_groups(artist, album)
    except ServiceError as exc:
        error = exc.message
    for rg in results:
        rg["artist_name"] = albums.artist_credit_name(rg)
    have = await owned.owned_release_groups([rg["id"] for rg in results], get_settings()) if results else {}
    return render(request, "search_results.html", artist=artist, album=album, results=results, error=error, have=have)


# Album page

@router.get("/album/{rg_mbid}", response_class=HTMLResponse)
async def album_page(request: Request, rg_mbid: str, search: bool = False):
    try:
        album = await albums.ensure_album(rg_mbid)
    except ServiceError as exc:
        return render(request, "error.html", message=f"Could not load this album from MusicBrainz: {exc.message}")
    if search:
        # Pick: search Soulseek straight away, unless a search is running or has just finished.
        latest = latest_search(rg_mbid)
        recent = latest and latest.finished_at and utcnow() - latest.finished_at < RESEARCH_AFTER
        if not latest or not (latest.state in ACTIVE_SEARCH or recent):
            start_search(rg_mbid)
        return RedirectResponse(f"/album/{rg_mbid}", status_code=303)  # a refresh doesn't search again
    have = await owned.check_album(rg_mbid, get_settings())
    editions, _ = (await _editions(rg_mbid)) if have and have.release_mbid else ([], None)
    return render(request, "album.html", album=album, search=latest_search(rg_mbid), have=have,
                  have_edition=next((e for e in editions if e.id == have.release_mbid), None) if have else None)


async def _editions(rg_mbid: str) -> tuple[list[Edition], str | None]:
    try:
        return await albums.album_editions(rg_mbid), None
    except ServiceError as exc:
        return [], exc.message


@router.get("/album/{rg_mbid}/editions", response_class=HTMLResponse)
async def album_editions_panel(request: Request, rg_mbid: str):
    editions, error = await _editions(rg_mbid)
    with session() as s:
        album = s.get(Album, rg_mbid)
    return render(request, "_editions.html", album=album, editions=editions, error=error)


@router.post("/album/{rg_mbid}/target/{release_mbid}", response_class=HTMLResponse)
async def album_toggle_target(request: Request, rg_mbid: str, release_mbid: str):
    """Pin (or unpin) the target edition; the candidates table refreshes itself."""
    with session() as s:
        album = s.get(Album, rg_mbid)
        if album is None:
            raise HTTPException(404)
        album.target_release = None if album.target_release == release_mbid else release_mbid
        s.add(album)
        s.commit()
    response = await album_editions_panel(request, rg_mbid)
    response.headers["HX-Trigger"] = "candidates-reload"
    return response


@router.post("/album/{rg_mbid}/search", response_class=HTMLResponse)
async def album_start_search(request: Request, rg_mbid: str):
    search = latest_search(rg_mbid)
    if not search or search.state not in ACTIVE_SEARCH:
        start_search(rg_mbid)
    return await album_candidates(request, rg_mbid)


async def _candidates_context(rg_mbid: str, params: dict, *, with_editions: bool = True) -> dict:
    """Everything the candidates section, its rows and its poll state need."""
    search = latest_search(rg_mbid)
    with session() as s:
        album = s.get(Album, rg_mbid)
    target = album.target_release if album else None

    def qs(**overrides) -> str:
        merged = {**params, **overrides}
        return urlencode({k: ("true" if v is True else v) for k, v in merged.items() if v not in ("", False, None)})

    candidates, hidden, sources, depths = [], 0, [], []
    searching_now = search is not None and search.state in ACTIVE_SEARCH
    downloads = {d.candidate_id: d for d in downloads_for_search(search.id)} if search else {}
    imports = importing.imports_for([d.id for d in downloads.values()])
    # Keep polling while anything shown in a row can still change.
    active = searching_now or (search is not None and (
        prefetching.active_prefetches(search.id)
        or any(d.state in ACTIVE_DOWNLOAD for d in downloads.values())
        or any(i.state in ("verifying", "refetching", "importing") for i in imports.values())
    ))
    # While searching there is nothing to label yet; skip the (cached) editions lookup.
    editions = {}
    if with_editions and search and not searching_now:
        editions = {e.id: e for e in (await _editions(rg_mbid))[0]}
    settings = get_settings()
    if search:
        unfiltered, hidden = ordered_candidates(search, show_all=params["all"], editions=editions)
        sources = sorted({parsed(c).get("source") or "unknown" for c in unfiltered})
        depths = sorted({d for c in unfiltered if (d := quality(c)[0])})
        candidates, _ = ordered_candidates(
            search, show_all=params["all"], source=params["source"] or None,
            depth=int(params["depth"]) if params["depth"].isdigit() else None,
            sort=params["sort"] or None, reverse=params["rev"], target=target, editions=editions,
        )

    if not search:
        phase = "none"
    elif search.state == "failed" or (searching_now and not candidates):
        phase = search.state
    else:
        phase = "rows" if candidates else "empty"

    def row_version(c: Candidate) -> str:
        """Changes whenever anything a row shows can have changed."""
        dl = downloads.get(c.id)
        imp = imports.get(dl.id) if dl else None
        pct = int(20 * dl.bytes_done / (dl.bytes_total or 1)) if dl else None
        state = [c.match, c.log_info, dl and (dl.state, pct, dl.chunked), imp and imp.state]
        return hashlib.sha1(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:12]

    return {
        "rg_mbid": rg_mbid, "search": search, "candidates": candidates, "hidden": hidden, "params": params, "qs": qs,
        "sources": sources, "depths": depths, "downloads": downloads, "parsed": parsed, "quality": quality,
        "match_of": match_of, "editions": editions, "target": editions.get(target) if target else None,
        "target_id": target, "rip_quality": rip_quality, "completeness": completeness, "active": active,
        "searching_now": searching_now, "disc_id_releases": prefetching.disc_id_releases,
        "phase": phase, "row_version": row_version,
        "pref": lambda c: pref_score(c, editions, settings),
    }


def _params(all: bool, source: str, depth: str, sort: str, rev: bool) -> dict:
    return {"all": all, "source": source, "depth": depth, "sort": sort, "rev": rev}


@router.post("/album/{rg_mbid}/search-more", response_class=HTMLResponse)
async def album_search_more(request: Request, rg_mbid: str):
    """A longer search for the same album; new folders join the table."""
    search = latest_search(rg_mbid)
    if search and search.state not in ACTIVE_SEARCH:
        search_more(search)
    return await album_candidates(request, rg_mbid)


@router.get("/album/{rg_mbid}/candidates", response_class=HTMLResponse)
async def album_candidates(
    request: Request, rg_mbid: str, all: bool = False, source: str = "", depth: str = "", sort: str = "", rev: bool = False,
):
    ctx = await _candidates_context(rg_mbid, _params(all, source, depth, sort, rev))
    return render(request, "_candidates.html", **ctx)


@router.get("/album/{rg_mbid}/candidates/state")
async def album_candidates_state(
    request: Request, rg_mbid: str, all: bool = False, source: str = "", depth: str = "", sort: str = "", rev: bool = False,
):
    """For static/candidates.js: row ids and versions in display order, without rendering rows."""
    ctx = await _candidates_context(rg_mbid, _params(all, source, depth, sort, rev), with_editions=False)
    status = templates.env.get_template("_candidates_status.html").render(ctx)
    return {
        "phase": ctx["phase"],
        "active": ctx["active"],
        "interval": 2000 if ctx["searching_now"] else 4000,
        "status_html": status,
        "rows": [[c.id, ctx["row_version"](c)] for c in ctx["candidates"]],
    }


@router.get("/album/{rg_mbid}/candidates/rows", response_class=HTMLResponse)
async def album_candidates_rows(
    request: Request, rg_mbid: str, ids: str = "",
    all: bool = False, source: str = "", depth: str = "", sort: str = "", rev: bool = False,
):
    """Rendered <tbody> for each requested candidate id."""
    ctx = await _candidates_context(rg_mbid, _params(all, source, depth, sort, rev))
    wanted = {int(x) for x in ids.split(",") if x.strip().isdigit()}
    row = templates.env.get_template("_candidate_row.html")
    html = "".join(row.render({**ctx, "c": c}) for c in ctx["candidates"] if c.id in wanted)
    return HTMLResponse(html)


@router.get("/album/{rg_mbid}/events", response_class=HTMLResponse)
async def album_events(request: Request, rg_mbid: str):
    with session() as s:
        events = s.exec(
            select(JobEvent).where(JobEvent.rg_mbid == rg_mbid).order_by(JobEvent.id.desc()).limit(50)
        ).all()
    return render(request, "_events.html", events=events)


# Downloads

def _candidate(candidate_id: int) -> tuple[Candidate, Search]:
    with session() as s:
        candidate = s.get(Candidate, candidate_id)
        if candidate is None:
            raise HTTPException(404)
        return candidate, s.get(Search, candidate.search_id)


@router.get("/candidates/{candidate_id}/confirm", response_class=HTMLResponse)
async def candidate_confirm(request: Request, candidate_id: int):
    """Inline form: confirm the edition before downloading. The best match is preselected."""
    candidate, search = _candidate(candidate_id)
    editions, _ = await _editions(search.rg_mbid)
    likely = match_of(candidate).get("editions") or []
    with session() as s:
        album = s.get(Album, search.rg_mbid)
    settings = get_settings()
    by_id = {e.id: e for e in editions}
    preferred = preferences.best_edition([by_id[r] for r in likely if r in by_id], settings)
    if album and album.target_release in likely:
        preselect = album.target_release
    else:
        preselect = preferred.id if preferred else (likely[0] if likely else None)
    ordered = [e for e in editions if e.id in likely] + [e for e in editions if e.id not in likely]
    return render(request, "_confirm.html", c=candidate, editions=ordered, likely=set(likely), preselect=preselect,
                  avoid_remasters=settings.avoid_remasters)


@router.get("/candidates/{candidate_id}/download-button", response_class=HTMLResponse)
async def candidate_download_button(request: Request, candidate_id: int):
    candidate, _ = _candidate(candidate_id)
    return render(request, "_download_button.html", c=candidate)


@router.post("/candidates/{candidate_id}/download", response_class=HTMLResponse)
async def candidate_download(request: Request, candidate_id: int):
    candidate, search = _candidate(candidate_id)
    form = await request.form()
    release_mbid = str(form.get("release_mbid") or "") or None
    with session() as s:
        existing = s.exec(
            select(Download).where(Download.candidate_id == candidate_id, Download.state.in_(ACTIVE_DOWNLOAD))
        ).first()
    dl = existing or start_download(candidate, search.rg_mbid, release_mbid)
    response = render(request, "_download_status.html", dl=dl)
    response.headers["HX-Trigger"] = "candidates-changed"  # keep the row's status updating
    return response


@router.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request):
    return render(request, "downloads.html", **_downloads_context())


@router.get("/downloads/table", response_class=HTMLResponse)
async def downloads_table(request: Request):
    return render(request, "_downloads_table.html", **_downloads_context())


def _downloads_context() -> dict:
    with session() as s:
        rows = s.exec(select(Download).order_by(Download.id.desc()).limit(50)).all()
        album_rows = {a.rg_mbid: a for a in s.exec(select(Album))}
    imports = importing.imports_for([d.id for d in rows])
    return {
        "rows": rows,
        "albums": album_rows,
        "imports": imports,
        "file_counts": lambda d: transfer_summary(d.file_states or {}),
        "retry_errors": lambda d: "\n".join(
            f"{n.rsplit(chr(92), 1)[-1]}: {st.get('error')}" for n, st in (d.file_states or {}).items()
            if st.get("status") == "retry_wait"
        ),
        "queue_timeout_hours": get_settings().queue_timeout_hours,
        "active": any(d.state in ACTIVE_DOWNLOAD for d in rows)
        or any(i.state in ("verifying", "refetching", "importing") for i in imports.values()),
    }


@router.post("/downloads/{download_id}/retry-failed")
async def download_retry_failed(download_id: int):
    """A partial download: request its failed files again (fresh retries), keep what arrived."""
    if retry_failed_files(download_id) is None:
        raise HTTPException(409, "only a partial download's failed files can be retried")
    return RedirectResponse("/downloads", status_code=303)


@router.post("/downloads/{download_id}/giveup")
async def download_give_up(download_id: int):
    """Stop waiting on this folder now and start the next-best one for the same edition."""
    request_give_up(download_id)
    return await download_next(download_id)


@router.post("/downloads/{download_id}/next")
async def download_next(download_id: int):
    """Start the next-best folder for the same edition (after a failure or a Reject)."""
    with session() as s:
        dl = s.get(Download, download_id)
        if dl is None:
            raise HTTPException(404)
        tried = {d.candidate_id for d in s.exec(select(Download).where(Download.search_id == dl.search_id))}
    candidate = next_best_candidate(dl.search_id, tried, dl.release_mbid)
    if candidate is None:
        return RedirectResponse(f"/album/{dl.rg_mbid}?no_next=1", status_code=303)
    start_download(candidate, dl.rg_mbid, dl.release_mbid)
    return RedirectResponse("/downloads", status_code=303)


# Rip logs, Lidarr feed

@router.post("/candidates/{candidate_id}/prefetch", response_class=HTMLResponse)
async def candidate_prefetch(request: Request, candidate_id: int):
    candidate, search = _candidate(candidate_id)
    prefetching.start_prefetch(candidate, search.rg_mbid)
    response = HTMLResponse('<span class="small muted">fetching log…</span>')
    response.headers["HX-Trigger"] = "candidates-changed"
    return response


@router.post("/lidarr/stuck/refresh")
async def stuck_refresh():
    lidarr_feed.queue_stuck_refresh()
    return RedirectResponse("/", status_code=303)


@router.post("/hooks/lidarr")
async def lidarr_webhook(request: Request):
    """Lidarr's Webhook connection (On Release Import, On Upgrade), with HTTP Basic auth."""
    settings = get_settings()
    if not settings.webhook_password:
        return JSONResponse({"error": "set webhook_password in the picker's settings first"}, status_code=503)
    user, password = _basic_auth(request.headers.get("authorization", ""))
    if not (secrets.compare_digest(user, settings.webhook_username)
            and secrets.compare_digest(password, settings.webhook_password)):
        return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Basic"})
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "expected a JSON body"}, status_code=400)
    if isinstance(payload, dict):
        probe.save_webhook(settings, payload)  # for the probe to check the payload shape
    return {"result": lidarr_feed.receive_webhook(payload)}


def _basic_auth(header: str) -> tuple[str, str]:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return "", ""
    try:
        user, _, password = base64.b64decode(value).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return "", ""
    return user, password


# Review and history

def _import(import_id: int) -> Import:
    with session() as s:
        imp = s.get(Import, import_id)
    if imp is None:
        raise HTTPException(404)
    return imp


@router.get("/imports/{import_id}", response_class=HTMLResponse)
async def import_page(request: Request, import_id: int, error: str | None = None):
    imp = _import(import_id)
    with session() as s:
        album = s.get(Album, imp.rg_mbid)
        dl = s.get(Download, imp.download_id) if imp.download_id else None
    editions, _ = await _editions(imp.rg_mbid)
    by_id = {e.id: e for e in editions}
    likely = []
    if dl:
        with session() as s:
            cand = s.get(Candidate, dl.candidate_id)
        likely = match_of(cand).get("editions") or [] if cand else []
    return render(
        request, "import.html", imp=imp, album=album, dl=dl, edition=by_id.get(imp.release_mbid),
        hold=importing.review_reason(imp, get_settings()),
        tagged=by_id.get((imp.tag_check or {}).get("tagged_mbid") or ""), by_id=by_id,
        editions=[e for e in editions if e.id in likely] + [e for e in editions if e.id not in likely],
        likely=set(likely), error=error, active=imp.state in ("verifying", "refetching", "importing"),
    )


@router.get("/imports/{import_id}/spectrogram/{name}")
async def import_spectrogram(import_id: int, name: str):
    if name not in _import(import_id).spectrograms:
        raise HTTPException(404)
    return FileResponse(get_settings().spectrogram_dir / str(import_id) / name, media_type="image/png")


@router.post("/imports/{import_id}/{action}")
async def import_action(request: Request, import_id: int, action: str):
    imp = _import(import_id)
    form = await request.form()
    back = "/review" if form.get("back") == "review" else f"/imports/{import_id}"
    try:
        if action == "import":
            importing.approve(imp)
        elif action == "force":
            importing.force_apply(imp)
        elif action == "retry":
            importing.retry(imp)
        elif action == "refetch":
            importing.refetch(imp)
        elif action == "edition":
            importing.change_edition(imp, str(form.get("release_mbid") or ""))
        elif action == "reject":
            importing.reject(imp)
            if imp.download_id:
                return RedirectResponse(f"/downloads/{imp.download_id}/next", status_code=307)
        else:
            raise HTTPException(404)
    except importing.Blocked as exc:
        return RedirectResponse(f"{back}?{urlencode({'error': str(exc)})}", status_code=303)
    return RedirectResponse(back, status_code=303)


@router.get("/review", response_class=HTMLResponse)
async def review_page(request: Request, error: str | None = None):
    """Every import waiting on the user, with its checks and the buttons inline."""
    imports = importing.waiting_imports()
    settings = get_settings()
    with session() as s:
        album_rows = {a.rg_mbid: a for a in s.exec(select(Album).where(Album.rg_mbid.in_({i.rg_mbid for i in imports})))}
    editions: dict[str, dict[str, Edition]] = {}
    for rg in {i.rg_mbid for i in imports if i.release_mbid}:
        eds, _ = await _editions(rg)
        editions[rg] = {e.id: e for e in eds}
    rows = [{"imp": imp, "album": album_rows.get(imp.rg_mbid), "hold": importing.review_reason(imp, settings),
             "edition": editions.get(imp.rg_mbid, {}).get(imp.release_mbid)} for imp in imports]
    return render(request, "review.html", rows=rows, error=error, auto_import=settings.auto_import)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    with session() as s:
        rows = s.exec(select(Import).order_by(Import.id.desc()).limit(200)).all()
        album_rows = {a.rg_mbid: a for a in s.exec(select(Album))}
    return render(request, "history.html", rows=rows, albums=album_rows)


# Settings

FORM_SECTIONS = {
    "Paths": ["download_dir", "music_dir", "port"],
    "slskd": ["slskd_url", "slskd_api_key"],
    "Lidarr": ["lidarr_url", "lidarr_api_key", "webhook_username", "webhook_password"],
    "Plex": ["plex_url", "plex_token", "plex_section_id", "plex_music_path"],
    "autoscan": ["autoscan_url", "autoscan_username", "autoscan_password"],
    "MusicBrainz": ["mb_contact"],
    "Search": ["search_timeout", "search_response_limit", "search_file_limit", "min_flac_folders",
               "search_quick_seconds", "search_quick_responses", "search_max_seconds"],
    "Preferences": [
        "flac_only", "prefer_original", "auto_import", "stuck_days", "stall_timeout_minutes", "queue_timeout_hours",
        "download_scans", "max_sidecar_mb", "transcode_check", "cutoff_db",
    ],
    "Edition preferences": ["avoid_remasters", "preferred_countries", "preferred_sources", "preferred_depth",
                            "avoid_flags", "prefer_flags"],
    "Lidarr defaults for new artists": [
        "lidarr_root_folder", "lidarr_quality_profile_id", "lidarr_metadata_profile_id",
        "lidarr_monitor", "lidarr_monitor_new_items",
    ],
    "Import": ["album_folder_template", "beets_timeout_minutes"],
}
SECTION_SERVICE = {"slskd": "slskd", "Lidarr": "lidarr", "Plex": "plex", "autoscan": "autoscan", "MusicBrainz": "musicbrainz"}


def _field_type(name: str) -> str:
    annotation = Settings.model_fields[name].annotation
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    return "secret" if name in SECRET_FIELDS else "text"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = False, error: str | None = None):
    return render(
        request, "settings.html",
        settings=get_settings(), sections=FORM_SECTIONS, section_service=SECTION_SERVICE,
        field_type=_field_type, locked=env_locked(), saved=saved, error=error,
        slskd_dir_warning=health.slskd_dir_warning,
    )


@router.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    updates: dict = {}
    for names in FORM_SECTIONS.values():
        for name in names:
            kind = _field_type(name)
            if kind == "bool":
                updates[name] = name in form
            elif name in form:
                value = str(form[name]).strip()
                if kind == "secret" and not value:
                    continue  # blank secret field means "keep the current one"
                updates[name] = int(value) if kind == "int" and value else value
    try:
        save_settings(updates)
    except (ValidationError, ValueError) as exc:
        return await settings_page(request, error=str(exc).splitlines()[0])
    await reset_services()
    return RedirectResponse("/settings?saved=1", status_code=303)


EXAMPLE_FOLDER = FolderValues(
    album_title="OK Computer", release_year="1997", artist_name="Radiohead", album_type="Album",
    album_mbid="b1392450-e666-3926-a536-22c65f834433", artist_mbid="a74b1b7f-71a5-4011-9441-d0b5e4122711",
    catalog_number="CDP 7243 8 55229 2 5", label="Capitol Records", country="US", media="CD", edition_date="1997-07-01",
)


@router.post("/settings/folder-preview", response_class=HTMLResponse)
async def settings_folder_preview(request: Request):
    """What `album_folder_template` would name the last few imported albums (or an example)."""
    form = await request.form()
    template = str(form.get("album_folder_template") or "").strip() or Settings.model_fields["album_folder_template"].default
    settings = get_settings()
    bad = unknown_tokens(template)
    if bad:
        return HTMLResponse(f'<p class="small warn-text">Unknown token{"s" if len(bad) > 1 else ""}: {escape(", ".join(bad))}</p>')
    with session() as s:
        imports = s.exec(select(Import).where(Import.release_mbid.is_not(None), Import.state == "imported")
                         .order_by(Import.id.desc()).limit(3)).all()
        albums_by_rg = {a.rg_mbid: a for a in s.exec(select(Album).where(Album.rg_mbid.in_([i.rg_mbid for i in imports]))).all()}
    rows = []
    for imp in imports:
        album = albums_by_rg.get(imp.rg_mbid)
        if album is None:
            continue
        try:
            values = await importing.folder_values(imp, album, settings)
        except ServiceError:
            continue
        rows.append((album.artist, album_folder(template, values), "would stay" if imp.album_dir and Path(imp.album_dir).name == album_folder(template, values) else "would change"))
    if not rows:
        rows.append((EXAMPLE_FOLDER.artist_name, album_folder(template, EXAMPLE_FOLDER), "example"))
    items = "".join(f'<li><span class="mono">{escape(artist)} / {escape(name)}</span> <span class="muted">({escape(note)})</span></li>'
                    for artist, name, note in rows)
    hint = ("from your last imports; existing folders are never renamed" if rows[0][2] != "example"
            else "no imports yet, so an example album")
    return HTMLResponse(f'<ul class="small folder-preview">{items}</ul><p class="small muted">{hint}.</p>')


@router.post("/settings/probe", response_class=HTMLResponse)
async def settings_probe():
    """Run the probe (takes up to a minute: it waits for one small Soulseek search)."""
    report = await probe.run_probe(get_settings())
    return HTMLResponse(f'<pre class="output">{escape(report.text())}</pre>')


@router.get("/settings/probe", response_class=HTMLResponse)
async def settings_probe_latest():
    latest = get_settings().config_dir / "probe" / "latest.txt"
    try:
        report = (Path(latest.read_text(encoding="utf-8").strip()) / "report.txt").read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse('<p class="muted small">No probe has run yet.</p>')
    return HTMLResponse(f'<pre class="output">{escape(report)}</pre>')


@router.post("/settings/test/{service}", response_class=HTMLResponse)
async def settings_test(request: Request, service: str):
    if service not in health.SERVICES:
        raise HTTPException(404)
    result = await health.test_service(service, get_settings())
    warning = health.slskd_dir_warning if service == "slskd" else None
    return render(request, "_test_result.html", result=result, slskd_dir_warning=warning)
