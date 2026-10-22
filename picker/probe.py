"""Check the real services' API shapes against what the picker reads.

Runs only non-destructive calls, saves each raw JSON reply under
/config/probe/<timestamp>/ (with anything that looks like a secret scrubbed), then
compares the replies with the fields the code relies on and writes report.txt.

    docker exec picker python -m picker probe

or the Probe button on the Settings page. The only thing it creates is one small
Soulseek search, which it deletes again afterwards. Webhook payloads Lidarr sends to
/hooks/lidarr are saved under /config/probe/webhooks/ and checked too.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import httpx

from .clients import services
from .clients.http import ServiceError, request
from .config import Settings
from .naming import unknown_tokens

SECRET_WORDS = ("apikey", "api_key", "password", "token", "secret", "passphrase")
PROBE_SEARCH = "radiohead ok computer"
KEEP_WEBHOOKS = 20


@dataclass
class Finding:
    level: str  # ok | mismatch | warn | error
    area: str
    message: str


@dataclass
class Report:
    out_dir: Path
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, area: str, message: str) -> None:
        self.findings.append(Finding(level, area, message))

    def text(self) -> str:
        counts = {lvl: sum(1 for f in self.findings if f.level == lvl) for lvl in ("ok", "mismatch", "warn", "error")}
        lines = [f"Picker probe {self.out_dir.name}: {counts['ok']} ok, {counts['mismatch']} mismatch, "
                 f"{counts['warn']} warn, {counts['error']} error", f"Raw JSON: {self.out_dir}", ""]
        lines += [f"{f.level.upper():8} {f.area}: {f.message}" for f in self.findings]
        return "\n".join(lines) + "\n"


def scrub(data):
    """Replace values of secret-looking keys, recursively."""
    if isinstance(data, dict):
        return {k: ("<redacted>" if any(w in k.lower().replace("-", "_") for w in SECRET_WORDS) and v else scrub(v))
                for k, v in data.items()}
    if isinstance(data, list):
        return [scrub(v) for v in data]
    return data


def get_path(data, path: str) -> list:
    """Values at a dotted path; `[]` fans out over a list: 'records[].artist.foreignArtistId'."""
    values = [data]
    for part in path.split("."):
        fan = part.endswith("[]")
        key = part[:-2] if fan else part
        nxt = []
        for v in values:
            if key:
                if not isinstance(v, dict) or key not in v:
                    continue
                v = v[key]
            if fan:
                nxt.extend(v if isinstance(v, list) else [])
            else:
                nxt.append(v)
        values = nxt
    return values


def expect(report: Report, area: str, data, path: str, types: tuple, *, optional: bool = False, sample: bool = True) -> list:
    """Record whether `path` exists with one of `types` (only checked if the parent exists)."""
    parent = path.rsplit(".", 1)[0] if "." in path else ""
    parents = get_path(data, parent) if parent else [data]
    values = get_path(data, path)
    if not parents:
        report.add("warn", area, f"{path}: nothing to check (no {parent or 'data'} in the reply)")
        return values
    if not values:
        if not optional:
            report.add("mismatch", area, f"{path}: missing (the code reads it)")
        return values
    bad = [v for v in values if not isinstance(v, types)]
    names = "/".join(t.__name__ for t in types)
    if bad:
        report.add("mismatch", area, f"{path}: expected {names}, got {type(bad[0]).__name__} {str(bad[0])[:60]!r}")
    elif sample:
        report.add("ok", area, f"{path}: {names}" + (f", e.g. {str(values[0])[:60]!r}" if values else ""))
    return values


class Prober:
    def __init__(self, settings: Settings, out_dir: Path):
        self.settings = settings
        self.report = Report(out_dir)
        self.out = out_dir

    def save(self, name: str, data) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / f"{name}.json").write_text(json.dumps(scrub(data), indent=2, default=str), encoding="utf-8")

    async def call(self, area: str, name: str, client: httpx.AsyncClient, service: str, method: str, path: str, **kw):
        try:
            resp = await request(client, service, method, path, retries=1, **kw)
        except ServiceError as exc:
            self.report.add("error", area, f"{method} {path}: {exc.message}")
            return None
        data = resp.json() if resp.content else None
        self.save(name, {"_status": resp.status_code, "_request": f"{method} {path}", "body": data})
        return data

    async def run(self) -> Report:
        for area, fn, configured in (
            ("slskd", self.slskd, bool(self.settings.slskd_url and self.settings.slskd_api_key)),
            ("lidarr", self.lidarr, bool(self.settings.lidarr_url and self.settings.lidarr_api_key)),
            ("plex", self.plex, bool(self.settings.plex_url and self.settings.plex_token)),
            ("autoscan", self.autoscan, bool(self.settings.autoscan_url)),
        ):
            if not configured:
                self.report.add("warn", area, "not configured; skipped")
                continue
            try:
                await fn()
            except Exception as exc:  # noqa: BLE001 - one service failing must not hide the others
                self.report.add("error", area, f"probe crashed: {type(exc).__name__}: {exc}")
        self.webhooks()
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "report.txt").write_text(self.report.text(), encoding="utf-8")
        (self.out.parent / "latest.txt").write_text(str(self.out), encoding="utf-8")
        return self.report

    # slskd ----------------------------------------------------------------------------

    async def slskd(self) -> None:
        r, http, a = self.report, services().slskd.http, "slskd"
        app = await self.call(a, "slskd_application", http, "slskd", "GET", "/application")
        if app is not None:
            version = get_path(app, "version.current") or get_path(app, "version.full") or get_path(app, "version")
            r.add("ok" if version else "mismatch", a, f"version {version[0] if version else 'not found at version.current'}")

        options = await self.call(a, "slskd_options", http, "slskd", "GET", "/options")
        if options is not None:
            downloads = get_path(options, "directories.downloads")
            if not downloads:
                r.add("mismatch", a, "directories.downloads missing in /options")
            elif PurePosixPath(downloads[0]) != PurePosixPath(self.settings.download_dir.as_posix()):
                r.add("warn", a, f"slskd downloads to {downloads[0]} but DOWNLOAD_DIR is {self.settings.download_dir}")
            else:
                r.add("ok", a, f"slskd download dir matches DOWNLOAD_DIR ({downloads[0]})")
            rfm = get_path(options, "remoteFileManagement")
            r.add("ok", a, f"remoteFileManagement = {rfm[0] if rfm else 'not shown'}")

        # One small search; deleted afterwards.
        search_id = str(uuid.uuid4())
        body = {"id": search_id, "searchText": PROBE_SEARCH, "searchTimeout": 5000, "responseLimit": 25, "fileLimit": 2000}
        if await self.call(a, "slskd_search_post", http, "slskd", "POST", "/searches", json=body) is None:
            return
        state, deadline = None, time.monotonic() + 90
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            state = await self.call(a, "slskd_search_state", http, "slskd", "GET", f"/searches/{search_id}")
            if not state or state.get("isComplete"):
                break
        if state:
            for path, types in (("isComplete", (bool,)), ("state", (str,)), ("responseCount", (int,)), ("fileCount", (int,))):
                expect(r, a, state, path, types)
        responses = await self.call(a, "slskd_search_responses", http, "slskd", "GET", f"/searches/{search_id}/responses")
        if isinstance(responses, list):
            self.save("slskd_search_responses", responses[:10])  # keep the file small
            sample = {"responses": responses[:25]}
            for path, types, opt in (
                ("responses[].username", (str,), False), ("responses[].hasFreeUploadSlot", (bool,), False),
                ("responses[].queueLength", (int,), False), ("responses[].uploadSpeed", (int,), False),
                ("responses[].files[].filename", (str,), False), ("responses[].files[].size", (int,), False),
                ("responses[].files[].bitDepth", (int,), True), ("responses[].files[].sampleRate", (int,), True),
                ("responses[].files[].length", (int,), True),
            ):
                expect(r, a, sample, path, types, optional=opt)
            if "\\" not in json.dumps([f.get("filename", "") for resp in responses[:5] for f in resp.get("files", [])[:3]]):
                r.add("warn", a, "remote paths in the responses don't use backslashes; check grouping")
        try:
            await request(http, "slskd", "DELETE", f"/searches/{search_id}", retries=0, ok_statuses=(404,))
        except ServiceError:
            r.add("warn", a, f"could not delete the probe search {search_id}; remove it in slskd's UI if you like")

        downloads = await self.call(a, "slskd_downloads", http, "slskd", "GET", "/transfers/downloads")
        if isinstance(downloads, list):
            transfers = get_path({"u": downloads}, "u[].directories[].files[]")
            self.save("slskd_downloads", downloads[:5])
            if not transfers:
                r.add("warn", a, "no transfers in slskd right now: download something, then probe again to check them")
            for path, types, opt in (
                ("t[].id", (str,), False), ("t[].filename", (str,), False), ("t[].state", (str,), False),
                ("t[].size", (int,), False), ("t[].bytesTransferred", (int,), False), ("t[].requestedAt", (str,), False),
                ("t[].batchId", (str,), True), ("t[].placeInQueue", (int,), True),
            ):
                if transfers:
                    expect(r, a, {"t": transfers}, path, types, optional=opt)
            batch_ids = [t["batchId"] for t in transfers if t.get("batchId")]
            if batch_ids:
                batch = await self.call(a, "slskd_batch", http, "slskd", "GET", f"/transfers/downloads/batches/{batch_ids[0]}")
                if batch is not None:
                    expect(r, a, batch, "id", (str,))
                    expect(r, a, batch, "transfers", (list,))
                    dest = get_path(batch, "options.destination")
                    r.add("ok", a, f"batch destination {dest[0] if dest else '(none)'}")
            queued = [t for t in transfers if "Queued" in (t.get("state") or "")]
            if queued:
                t = queued[0]
                pos = await self.call(a, "slskd_position", http, "slskd", "GET",
                                      f"/transfers/downloads/{t['username']}/{t['id']}/position")
                r.add("ok" if isinstance(pos, int) else "mismatch", a,
                      f"queue position reply is {type(pos).__name__} {pos!r} (the code expects a bare integer)")

    # Lidarr ---------------------------------------------------------------------------

    async def lidarr(self) -> None:
        r, http, a, s = self.report, services().lidarr.http, "lidarr", self.settings
        status = await self.call(a, "lidarr_status", http, "Lidarr", "GET", "/system/status")
        if status is not None:
            expect(r, a, status, "version", (str,))

        wanted = await self.call(a, "lidarr_wanted_missing", http, "Lidarr", "GET", "/wanted/missing",
                                 params={"page": 1, "pageSize": 5, "monitored": "true", "includeArtist": "true"})
        album_id = None
        if wanted is not None:
            for path, types, opt in (
                ("totalRecords", (int,), False), ("pageSize", (int,), False), ("records", (list,), False),
                ("records[].id", (int,), False), ("records[].foreignAlbumId", (str,), False),
                ("records[].releaseDate", (str,), True), ("records[].artist.artistName", (str,), False),
                ("records[].artist.foreignArtistId", (str,), False),
            ):
                expect(r, a, wanted, path, types, optional=opt)
            album_id = next(iter(get_path(wanted, "records[].id")), None)
        if album_id is None:
            artists = await self.call(a, "lidarr_artists", http, "Lidarr", "GET", "/artist")
            if artists:
                self.save("lidarr_artists", artists[:3])
                albums = await self.call(a, "lidarr_albums", http, "Lidarr", "GET", "/album", params={"artistId": artists[0]["id"]})
                album_id = albums[0]["id"] if albums else None
        if album_id is not None:
            album = await self.call(a, "lidarr_album", http, "Lidarr", "GET", f"/album/{album_id}")
            if album is not None:
                for path, types, opt in (
                    ("id", (int,), False), ("title", (str,), False), ("foreignAlbumId", (str,), False),
                    ("releaseDate", (str,), True), ("releases[].foreignReleaseId", (str,), False),
                    ("releases[].monitored", (bool,), False), ("statistics.trackFileCount", (int,), True),
                    ("statistics.trackCount", (int,), True),
                ):
                    expect(r, a, album, path, types, optional=opt)
                monitored = [x for x in album.get("releases", []) if x.get("monitored")]
                r.add("ok" if len(monitored) == 1 else "warn", a, f"album {album_id} has {len(monitored)} monitored release(s)")
                files = await self.call(a, "lidarr_trackfiles", http, "Lidarr", "GET", "/trackfile", params={"albumId": album_id})
                if files:
                    expect(r, a, {"f": files}, "f[].path", (str,))
        else:
            r.add("warn", a, "no album found to check releases[] (add an artist in Lidarr, then probe again)")

        meta = await self.call(a, "lidarr_metadataprovider", http, "Lidarr", "GET", "/config/metadataprovider")
        if meta is not None:
            tags = str(meta.get("writeAudioTags", "missing"))
            r.add("ok" if tags.lower() == "no" else "mismatch", a,
                  f"Write Audio Tags = {tags} (must be 'no': beets is the only tag writer)")

        naming = await self.call(a, "lidarr_naming", http, "Lidarr", "GET", "/config/naming")
        if naming is not None:
            self._check_naming(naming)

        roots = await self.call(a, "lidarr_rootfolders", http, "Lidarr", "GET", "/rootfolder")
        if isinstance(roots, list):
            paths = [x.get("path", "").rstrip("/") for x in roots]
            want = s.lidarr_root_folder.rstrip("/")
            r.add("ok" if want in paths else "mismatch", a, f"root folders {paths}; picker uses {want}")

        commands = await self.call(a, "lidarr_commands", http, "Lidarr", "GET", "/command")
        if isinstance(commands, list) and commands:
            for path, types in (("c[].id", (int,)), ("c[].name", (str,)), ("c[].status", (str,))):
                expect(r, a, {"c": commands[:20]}, path, types)
            statuses = sorted({c.get("status") for c in commands})
            r.add("ok", a, f"command statuses seen: {statuses} (the code waits for 'completed')")

    def _check_naming(self, naming: dict) -> None:
        r, a = self.report, "lidarr"
        rename = naming.get("renameTracks")
        r.add("ok" if rename else "warn", a, f"renameTracks = {rename}")
        template = self.settings.album_folder_template
        for key in ("standardTrackFormat", "multiDiscTrackFormat"):
            fmt = naming.get(key) or ""
            folder = fmt.replace("\\", "/").rsplit("/", 1)[0] if "/" in fmt.replace("\\", "/") else ""
            if not folder:
                r.add("warn", a, f"{key} has no folder part ({fmt!r}); Lidarr puts tracks straight in the artist folder")
            elif folder == template:
                r.add("ok", a, f"{key} folder part matches album_folder_template: {folder!r}")
            elif unknown_tokens(folder):
                r.add("mismatch", a, f"{key} folder part {folder!r} uses tokens the picker can't render: "
                                     f"{unknown_tokens(folder)}; picker template is {template!r}")
            else:
                r.add("mismatch", a, f"{key} folder part {folder!r} differs from album_folder_template {template!r}")
        if naming.get("replaceIllegalCharacters") is False:
            r.add("warn", a, "replaceIllegalCharacters is off: Lidarr deletes illegal characters, the picker replaces them")
        colon = naming.get("colonReplacementFormat")
        if colon not in (None, 4, "smart", "Smart"):
            r.add("warn", a, f"colonReplacementFormat = {colon!r}; the picker uses Lidarr's default (Smart)")

    # Plex -----------------------------------------------------------------------------

    async def plex(self) -> None:
        r, http, a, s = self.report, services().plex.http, "plex", self.settings
        sections = await self.call(a, "plex_sections", http, "Plex", "GET", "/library/sections")
        if sections is None:
            return
        dirs = get_path(sections, "MediaContainer.Directory[]")
        for path, types in (("MediaContainer.Directory[].key", (str,)), ("MediaContainer.Directory[].type", (str,)),
                            ("MediaContainer.Directory[].title", (str,))):
            expect(r, a, sections, path, types)
        mine = next((d for d in dirs if str(d.get("key")) == str(s.plex_section_id)), None)
        if mine is None:
            r.add("mismatch", a, f"no section with key {s.plex_section_id!r}; sections: "
                                 f"{[(d.get('key'), d.get('title'), d.get('type')) for d in dirs]}")
            return
        r.add("ok" if mine.get("type") == "artist" else "mismatch", a, f"section {mine.get('key')} is {mine.get('title')!r} ({mine.get('type')})")
        locations = [loc.get("path", "").rstrip("/") for loc in mine.get("Location", [])]
        plex_root = (s.plex_music_path or s.music_dir.as_posix()).rstrip("/")
        r.add("ok" if plex_root in locations else "mismatch", a,
              f"Plex library folders {locations}; partial scans will use {plex_root}/<artist>/<album>")

    # autoscan ---------------------------------------------------------------------------

    async def autoscan(self) -> None:
        try:
            status = await services().autoscan.check()
        except ServiceError as exc:
            self.report.add("error", "autoscan", exc.message)
            return
        self.report.add("ok", "autoscan", f"manual trigger reachable with these credentials (HTTP {status} without a folder; nothing queued)")
        self.report.add("ok", "autoscan", f"imports will ask for folders under {self.settings.music_dir.as_posix()}; "
                                          "autoscan must see the library at that same path")

    # Webhooks ---------------------------------------------------------------------------

    def webhooks(self) -> None:
        r, a = self.report, "webhook"
        folder = self.settings.config_dir / "probe" / "webhooks"
        files = sorted(folder.glob("*.json")) if folder.is_dir() else []
        if not files:
            r.add("warn", a, "no webhook received yet: press Test on Lidarr's Webhook connection, then probe again")
            return
        for path in files[-5:]:
            payload = json.loads(path.read_text(encoding="utf-8"))
            event = str(payload.get("eventType", "")).lower()
            r.add("ok", a, f"{path.name}: eventType {payload.get('eventType')!r}")
            if event == "download":
                expect(r, a, payload, "album.id", (int,))
                expect(r, a, payload, "album.mbId", (str,), optional=True)
                expect(r, a, payload, "trackFiles[].path", (str,))
                paths = get_path(payload, "trackFiles[].path")
                outside = [p for p in paths if not p.startswith(self.settings.music_dir.as_posix())]
                if outside:
                    r.add("mismatch", a, f"track paths outside MUSIC_DIR, e.g. {outside[0]}")


def save_webhook(settings: Settings, payload: dict) -> None:
    """Keep the last few webhook bodies for the probe to check."""
    folder = settings.config_dir / "probe" / "webhooks"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    event = "".join(ch for ch in str(payload.get("eventType", "unknown")) if ch.isalnum())[:20] or "unknown"
    unique = uuid.uuid4().hex[:6]  # two webhooks can arrive within one clock tick
    (folder / f"{stamp}-{unique}-{event}.json").write_text(json.dumps(scrub(payload), indent=2), encoding="utf-8")
    for old in sorted(folder.glob("*.json"))[:-KEEP_WEBHOOKS]:
        old.unlink(missing_ok=True)


async def run_probe(settings: Settings) -> Report:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return await Prober(settings, settings.config_dir / "probe" / stamp).run()
