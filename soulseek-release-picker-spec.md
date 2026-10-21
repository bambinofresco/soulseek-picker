# Soulseek release picker: Claude Code spec

Oct 21, 2026 · @Bill Hill

## Overview

The picker is a self-hosted web app that lets one person choose the exact edition of an album to download from Soulseek, then tags it to that edition with beets and files it into the Plex library. It is the hand-picking half of a music stack; Lidarr stays in place for artist monitoring and automatic grabs of new releases.

**Goals**

- For any album, show every Soulseek folder that has it, side by side with every known edition from MusicBrainz, and say which edition each folder most likely is.
- Surface what uploaders encode in paths and sidecar files: catalog numbers, labels, years, remaster or deluxe flags, source (CD, WEB, vinyl), bit depth and sample rate, presence of rip logs, cues and scans.
- Identify CD rips exactly when a rip log is present, by computing the MusicBrainz disc ID from the log's table of contents before downloading the album.
- Make beets the only thing that writes tags in the library, for both picker downloads and Lidarr's automatic imports.
- Keep Lidarr's view of the library in agreement with what beets tagged (artist present, same edition selected, folder rescanned).

**Non-goals**

- No automatic grabbing. The picker never downloads without a click; automatic grabs of new releases are Lidarr's job (Lidarr nightly with the slskd plugin, alongside Usenet).
- No Usenet or torrent support in the picker.
- No multi-user accounts. It is a single-user tool behind the home network or a reverse proxy.
- No audio playback or library browsing beyond what the workflow needs; Plexamp does that.

**Where it sits**

| Component | Owns | Never does |
| --- | --- | --- |
| Lidarr (nightly, slskd plugin) | Artist monitoring, automatic grabs from Usenet and Soulseek, placing its own downloads | Write audio tags (set to Never) |
| slskd (behind Gluetun, ProtonVPN) | Soulseek searches, browsing, transfers | Decide what to download |
| Picker (this project) | Hand-picked searches, edition matching, rip-log checks, import orchestration, Lidarr sync | Grab anything unattended |
| beets (inside the picker container) | All tag writing, pinned to one MusicBrainz release ID | Choose the edition itself |
| Plex / Plexamp | Reading tags and playing | Override local tags (Prefer local metadata on) |

## Architecture and tech stack

One Docker image, one Python process, one SQLite file. Everything slow (searches, downloads, MusicBrainz calls, imports) runs as a background job so the UI never blocks.

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 | beets, mutagen and MusicBrainz tooling are Python |
| Web | FastAPI + uvicorn | Async HTTP to slskd, Lidarr, MusicBrainz in parallel |
| UI | Jinja2 templates + HTMX, a little Alpine.js, plain CSS | No frontend build step; live-updating tables via HTMX polling |
| HTTP client | httpx (async) | One client per service, shared timeouts and retries |
| Storage | SQLite via SQLModel, file at `/config/picker.db` | Single user; jobs, caches and history survive restarts |
| Jobs | In-process asyncio worker reading a `jobs` table | No Redis or Celery; a restart resumes queued jobs |
| Tagging | beets installed in the same image, run as a subprocess with its own config and library at `/config/beets/` | Isolated, inspectable, same as running beets by hand |
| Audio tools | `flac`, `sox`, `ffprobe`, mutagen | Integrity check, spectrograms, reading existing tags |
| Packaging | Dockerfile + example compose and Unraid template | Runs beside slskd, Lidarr and Gluetun |

**Container contract**

- Port: `8765` (configurable).
- Volumes: `/config` (DB, beets config and library, logs, spectrogram cache), `/downloads/soulseek` (slskd's download directory, mounted at the same path as inside slskd), `/music` (the library root, same path as inside Lidarr and Plex where possible).
- User: run as `PUID`/`PGID` from env (Unraid default 99:100) so files land owned by `nobody:users`.
- Network: on the same Docker network as Lidarr and Gluetun. slskd is reached at `http://gluetun:5030` because slskd shares Gluetun's network stack.

**Configuration**

Environment variables override `/config/picker.yaml`. Required: slskd URL and API key; Lidarr URL and API key; Plex URL, token and music section ID; MusicBrainz contact string for the User-Agent; the three paths above. Secrets never appear in logs or the UI.

**Path mapping**

slskd, Lidarr and the picker must agree on paths. The picker validates at startup that `/downloads/soulseek` and `/music` exist and are writable, and shows a settings-page warning if slskd reports a download directory that the picker cannot see.

## Core flow

Every hand-picked download goes through the same nine steps, from choosing an album to a tagged folder in the library.

1. **Choose an album.** Three entry points, all ending in a MusicBrainz release-group ID plus artist ID:
   - Search box: artist and album text, resolved through a MusicBrainz release-group search; the user picks the right one.
   - Lidarr queue: albums Lidarr has had in Wanted for more than N days (default 3).
   - Direct paste of a MusicBrainz release-group or release URL.
2. **Load the editions.** Fetch every release in the release group with labels, media and track lists, and cache them for 7 days. Each becomes an Edition row: release ID, title, disambiguation, date, country, label(s), catalog number(s), barcode, format per medium, track count per medium, track lengths, status, packaging.
3. **Search Soulseek.** Build a primary query from artist and album (lower-cased, diacritics folded, punctuation and bracketed suffixes stripped, `the` dropped from the front). Run it through slskd and poll until the search completes. If fewer than 5 FLAC folders come back, run up to two fallback queries (album title alone when it is at least two distinctive words; artist plus the album's first strong word).
4. **Group into candidates.** A candidate is one user's folder: all files sharing a parent directory. Sibling folders named like `CD1`, `Disc 2` or `Disc 2 - Live` under one parent merge into one multi-disc candidate. Non-audio files in the folder (`.log`, `.cue`, `.jpg`, `.png`, `.txt`, `.nfo`, `.m3u`, `Scans/`) stay attached to the candidate.
5. **Filter.** Default view shows FLAC only and hides candidates with fewer than 3 audio files or under half the track count of the smallest edition. A toggle reveals everything.
6. **Parse and match.** Parse each candidate's paths (Folder metadata parsing), then score it against every edition (Edition matching).
7. **Show the table.** One row per candidate, best match first; details in the UI section. The user can sort by any column and filter by edition, source or bit depth.
8. **Prefetch logs.** For candidates that have a `.log` or `.cue`, download just those files and refine the match (Rip log and cue prefetch). Automatic for users with a free upload slot; a button for the rest.
9. **Download and import.** The user clicks Download on a row and confirms the edition (the best match is preselected and can be changed). The picker enqueues the folder's audio files plus its small sidecars (logs, cues, images under 15 MB each; scans folders optional), tracks the transfer, and on completion runs the import pipeline.

**Failure handling**

- A transfer that errors, or stalls for longer than the configurable stall timeout (default 30 minutes with no progress), marks the candidate failed and offers the next-best candidate for the same edition.
- A user who goes offline mid-transfer gets the same treatment; partial files stay in slskd's incomplete folder and are cleaned up by slskd, not by the picker.
- Every job writes a short, human-readable event log shown on the album's page.

## Folder metadata parsing

The parser turns a candidate's paths into structured hints, and every hint keeps the token it came from so the UI can show its evidence. It is a pure function (`parse_candidate(paths) -> ParsedFolder`) in its own module, driven by tables of patterns, with unit tests over a fixture corpus.

**Inputs**

- Every directory segment from the share root down to the candidate folder, not just the last one. Collectors often put the edition in a parent folder (`Artist/1987 - Album [CD] [CAT-1234]/CD1`).
- Audio file names, sidecar file names, and slskd's per-file attributes (bit depth, sample rate, bitrate, duration, size).

**What it extracts**

| Field | How | Watch out for |
| --- | --- | --- |
| Years | Four-digit 1900–2099 tokens, in brackets or leading a segment (` 1987 -  `). With two years, the earlier is the original and the later is the reissue, especially when the later sits next to a remaster word. | Track numbers and catalog digits that look like years |
| Catalog number tokens | Letters plus digits with an optional separator (`ABC-1234`, `CK 64935`, `5099 9 12345 2 6`), usually in brackets. Kept as *candidates*; only a match against an edition's catalog number makes one real. | `CD1`, `24-96`, `MP3-320`, `FLAC`, `16-44`, years |
| Barcode tokens | 12- or 13-digit runs | Long numeric file prefixes |
| Label | Fuzzy match of segment text against the label names of this album's editions only, never against a global list | Artist names that contain label words |
| Source | `CD`, `WEB` (and store names: Qobuz, Bandcamp, HDtracks, Tidal), `Vinyl` / `LP` / needle drop, `SACD`, `DVD-A`, `Blu-ray`, `Cassette` | `web` inside words |
| Bit depth and sample rate | slskd file attributes first; path tokens like `24-96`, `24bit`, `16-44.1` only as a fallback | Mixed folders: report the most common value and flag mixing |
| Remaster | Words: remaster, remastered, remastering, `RM`, plus a later year next to them | "Remastered" inside a track title; treat track-level mentions as weaker |
| Other edition markers | deluxe, expanded, anniversary, bonus, reissue, SHM-CD, Blu-spec, MFSL / Mobile Fidelity, DCC, gold, first press / 1st press, promo, OOP, mono, stereo, territory words (Japan, UK, US, Germany) | Album titles that contain these words ("Deluxe" as a real title) |
| Rip tool | EAC, XLD, dBpoweramp, CUERipper, whipper in paths; confirmed later from the log itself |  |
| Sidecars | Counts of `.log`, `.cue`, images, `Scans/`, `.txt` / `.nfo` |  |

**Output**

`ParsedFolder` holds each field as a value plus a list of `Evidence(token, segment, meaning)`. The UI shows the values as badges and the evidence on hover, so a wrong guess is easy to spot and report.

**Tests**

Ship `tests/fixtures/folder_names.yaml` with at least 80 hand-written folder paths covering every row above, including traps, each with its expected parse. The parser must pass all of them before matching is built on top.

## Edition matching and scoring

Each candidate is scored against every edition, and the result is a confidence tier plus the set of editions it could be, not a single forced guess. Many editions share a track list and lengths; when the evidence cannot tell them apart, the row says "one of these 3" and lists them.

**Signals, strongest first**

| Signal | Computed from | Effect |
| --- | --- | --- |
| Disc ID | TOC from a prefetched `.log` (next section) equals an edition's disc ID | Definitive for every edition carrying that disc ID |
| Catalog number | A parsed token equals an edition's catalog number after normalising (upper case, spaces, hyphens and dots removed) | Strong |
| Barcode | A parsed 12/13-digit token, or a `CATALOG` line in a cue, equals the edition's barcode | Strong |
| Track count | Audio files per disc against the edition's track count per medium | Required for High or Exact; a mismatch caps the tier at Low |
| Durations | slskd per-file length against MusicBrainz track lengths, in order: share of tracks within ±2 s and mean absolute difference | Strong when every track fits; skipped when the edition lacks lengths for over 20% of tracks |
| Year | Parsed original and reissue years against the edition's date | Medium |
| Label | Fuzzy label match | Medium |
| Source vs format | CD folders against CD editions, WEB against Digital Media, vinyl against vinyl | Medium; a contradiction is a strong negative |
| Remaster words | Path flags against the edition's title and disambiguation | Medium, both directions |
| Territory words | Japan, UK and similar against the edition's country | Weak |

**Tiers**

- **Exact:** disc ID match, or catalog number / barcode match with track count agreeing.
- **High:** track count and durations agree, plus at least one of year, label or source.
- **Medium:** track count agrees, plus one other signal.
- **Low:** track count only, or contradictory signals.
- **Unknown:** nothing usable.

The matcher returns, per candidate: the tier, the list of editions at the top score (the ambiguity set), and the signals that fired with their evidence. Start with additive weights in one config table and tune them against the fixture corpus; the tiers are the contract, the weights are not.

**Separate scores, shown as their own columns**

- **Availability:** free upload slot, queue length, upload speed.
- **Rip quality:** log present, log from a secure ripper, AccurateRip or checksum results once the log is read, cue present.
- **Completeness:** all tracks of the matched edition present, cover art present.

**Default ordering**

Sort by tier, then by the user's preference setting, then by availability. The preference setting `prefer_original` (default on) ranks the earliest-dated edition first within a tier and pushes remaster-flagged candidates below unflagged ones. The user can also pin a target edition, which filters the table to candidates whose ambiguity set includes it.

**Not judged before download**

Whether a FLAC is a lossy transcode cannot be judged from Soulseek metadata. The picker makes no claim about it until the spectrogram check after download.

## Rip log and cue prefetch

A CD rip log contains the disc's table of contents, from which the MusicBrainz disc ID can be computed exactly; the picker downloads just the `.log` and `.cue` files (a few KB) before the album to identify the edition. Treat a log as a strong bonus signal: many folders have none, and reissues that reuse a master can share a disc ID.

**Fetching**

- For each candidate with a `.log` or `.cue`, enqueue only those files through slskd, into `picker/prefetch/<candidate-id>/` under the download directory (the batch `destination` option, see API notes).
- Automatic for users with a free upload slot, at most 5 users at a time; a Fetch log button for the rest.
- Cache results by `(username, remote folder path)` so a repeat search doesn't refetch.

**Decoding and identifying the ripper**

- EAC logs are UTF-16LE with CRLF line endings; XLD logs are UTF-8 ([hbcl test logs](https://github.com/doujincafe/hbcl/tree/master/tests/logs)). Detect by BOM and null bytes, fall back to UTF-8 then Latin-1.
- Identify the ripper from the header line (Exact Audio Copy, X Lossless Decoder). Version 1 parses EAC and XLD tables of contents; logs from other rippers are recorded as "log present, not parsed".

**Parsing the table of contents**

Both formats have a block headed `TOC of the extracted CD` with columns Track, Start, Length, Start sector, End sector. EAC writes times as `m:ss.ff`, XLD as `MM:SS:FF`; only the sector columns are used. A file may hold more than one TOC block (multi-disc); parse each.

**Computing the disc ID** ([algorithm](https://musicbrainz.org/doc/Disc_ID_Calculation), [libdiscid](https://github.com/metabrainz/libdiscid/blob/master/src/disc.c))

1. Track offsets = start sector + 150 (the two-second pregap).
2. Lead-out = last audio track's end sector + 1 + 150.
3. Data tracks are not labelled in either log format. If the gap between the end of track N−1 and the start of track N is about 11,400 sectors, track N is a data track (Enhanced CD): drop it, and set the lead-out to its start sector + 150 − 11,400.
4. Build the string: first track as `%02X`, last audio track as `%02X`, then 100 values as `%08X` (slot 0 the lead-out, slots 1–99 the track offsets, 0 when unused): 804 upper-case hex characters.
5. SHA-1 it, then Base64-encode with `+`→`.`, `/`→`_`, `=`→`-`. The result is 28 characters.

**Looking it up**

- `GET /ws/2/discid/<id>?toc=<first>+<last>+<leadout>+<offsets…>&cdstubs=no&inc=labels+recordings+release-groups&fmt=json`. With `toc` supplied, an unknown ID falls back to a fuzzy TOC match.
- Keep the returned releases that belong to this album's release group; they become the disc ID signal in matching. Releases from other groups (a compilation, a box set) are shown as a note, not a match.

**What else the log tells us** (shown in the expanded row and used for the rip quality score)

- Read mode (secure or not), AccurateRip results per track, test and copy CRCs matching, the final "no errors" or "errors" line, and whether the log carries the ripper's checksum.

**What a cue adds**

- A `CATALOG` line (12- or 13-digit UPC/EAN) is a barcode signal. Per-track `ISRC` lines are stored for display. Cue index times are not used to rebuild a TOC.

**Tests**

Fixture logs covering: EAC UTF-16, XLD, an Enhanced CD with a data track, and a two-disc set, each with its expected disc ID. If the `discid` Python package and libdiscid are available in the test environment, use `discid.put()` as an independent check of the hand-written function.

## Import pipeline

A finished download ends up in Lidarr's folder for that artist, tagged by beets to the exact chosen release, with Lidarr and Plex both told. The picker places the folder itself and runs beets in place, so the folder always lands where Lidarr expects it.

1. **Download lands.** Downloads are enqueued with the batch `destination` set to `picker/<job-id>/`, so the files arrive at a known path. Completion is detected by polling the batch (or slskd's `DownloadDirectoryComplete` webhook, if configured). Success = every transfer in the batch is `Completed, Succeeded`.
2. **Verify.** File count equals what was enqueued; `flac -t` passes on every FLAC; mutagen can read every file. Any failure stops here and shows the reason.
3. **Review** (skipped when `auto_import` is on). Spectrograms of two tracks with `sox <file> -n spectrogram -o <png>`, cached under `/config/spectrograms/`. The user clicks Import, Change edition or Reject.
4. **Make sure Lidarr has the artist** (Lidarr integration, below). Read the artist's `path` from Lidarr.
5. **Place the folder.** Move `picker/<job-id>/` to `<artist path>/<album folder>`, where the album folder name comes from a template in settings (default `{album} ({year})`, set to match your Lidarr album folder format). Downloads and library share one filesystem, so this is a rename. If the target exists, stop and ask; never overwrite.
6. **Tag with beets, in place.** `beet -c /config/beets/config.yaml import -q --search-id <release MBID> "<album folder>"`, with the config below. Check the beets log and exit code: in quiet mode beets *skips* an album whose match isn't strong, even with a pinned ID ([beets source](https://github.com/beetbox/beets/blob/master/beets/ui/commands/import_/session.py)). On a skip, show beets' output and a Force apply button that reruns with an overlay config (`-c` a second file) setting `match.strong_rec_thresh: 1.0`.
7. **Sync Lidarr.** Select the same release on the Lidarr album, then rescan the artist folder (Lidarr integration, below). Confirm the album's track file count equals its track count.
8. **Tell Plex.** Partial scan of the album folder as Plex sees it (a path prefix mapping in settings translates `/music` to Plex's path).
9. **Record it.** History row with the edition, source user, folder, beets output and outcome.

**beets config** (`/config/beets/config.yaml`, generated on first run, editable)

```yaml
directory: /music
library: /config/beets/library.db
import:
  copy: no
  move: no
  write: yes
  quiet: yes
  quiet_fallback: skip
  duplicate_action: keep
  timid: no
  log: /config/beets/import.log
match:
  strong_rec_thresh: 0.10
musicbrainz:
  ratelimit: 1
plugins: fetchart
fetchart:
  auto: yes
```

Start `strong_rec_thresh` at 0.10 (beets' default is 0.04) because the release is already pinned; tune it against real imports.

**Tags this produces in FLAC files** ([mediafile](https://github.com/beetbox/mediafile/blob/master/mediafile/__init__.py)): `CATALOGNUMBER`, `LABEL`, `RELEASECOUNTRY`, `MEDIA`, `BARCODE`, `RELEASESTATUS`, `ORIGINALDATE`, `MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_RELEASEGROUPID`, `MUSICBRAINZ_ALBUMCOMMENT` (the edition's disambiguation), plus the standard artist, album and track fields.

**Never**

- Never overwrite or delete anything under `/music`.
- Never run beets with `move: yes` or `copy: yes`; the picker owns placement.

## Lidarr integration

The picker talks to Lidarr for four things: reading the stuck-wanted list, adding a missing artist, selecting the same edition beets tagged, and retagging Lidarr's own automatic imports. Lidarr's Write Tags setting stays at Never.

**1. Stuck-wanted queue**

- Every hour, page through `GET /api/v1/wanted/missing?monitored=true&includeArtist=true&pageSize=100`.
- Keep albums whose release date is more than `stuck_days` (default 3) in the past. Show them on Home, newest first.

**2. Make sure the artist exists**

1. `GET /api/v1/artist` and look for the artist's MusicBrainz ID (`foreignArtistId`).
2. If missing: `GET /api/v1/artist/lookup?term=lidarr:<artist MBID>`, then `POST /api/v1/artist` with that result plus `qualityProfileId`, `metadataProfileId`, `rootFolderPath`, `monitored`, `monitorNewItems`, and `addOptions: {monitor: <setting>, searchForMissingAlbums: false}`.
3. Defaults in settings: monitor `none`, `monitorNewItems` `new`, so new releases from that artist are watched but adding one old album never makes Lidarr search the back catalogue. `searchForMissingAlbums` is always false.
4. Wait for the artist's refresh to finish (poll `GET /api/v1/command/{id}`) before the next step, so the album exists in Lidarr.

**3. Select the edition**

1. `GET /api/v1/album?foreignAlbumId=<release-group MBID>`.
2. In its `releases`, set `monitored: true` on the release whose `foreignReleaseId` equals the chosen release MBID and `false` on all others. Exactly one may be true; Lidarr breaks if two are.
3. `PUT /api/v1/album/{id}` with the whole album. When the selection changes, Lidarr queues its own folder rescan.
4. Then `POST /api/v1/command` with `{"name": "RescanFolders", "folders": ["<artist path>"], "filter": "matched"}` (harmless if Lidarr already queued one).
5. If the chosen release isn't in the album's `releases` (the metadata profile filters it out), finish the import, flag it in history, and explain which profile setting excludes it.

**4. Retag Lidarr's own imports**

- In Lidarr, the user adds a Webhook connection (Settings → Connect) pointing at `http://picker:8765/hooks/lidarr`, triggers On Release Import and On Upgrade, with a username and password the picker checks.
- The payload's `eventType` is `Download` for an import (`Test` for the test button: reply 200). It carries `album.id`, `album.mbId` (the release-group ID) and `trackFiles[].path`, but **not** the release MBID ([Lidarr source](https://github.com/Lidarr/Lidarr/tree/develop/src/NzbDrone.Core/Notifications/Webhook)).
- So: `GET /api/v1/album/{album.id}`, take the release with `monitored: true`, and run the same in-place beets import pinned to it on the folder holding the track files. Then the Plex partial scan.
- Debounce by album ID for 60 seconds, so a multi-disc import that fires twice is retagged once.
- Lidarr and the picker both see the library at `/music`, so the paths in the payload are usable as-is.

**Settings this section needs:** Lidarr URL and API key, root folder, quality profile, metadata profile, monitor option, `monitorNewItems`, `stuck_days`, webhook username and password.

## UI

Five pages, desktop-first but usable on a phone; the album page is where nearly all the time is spent.

**Home**

- Search box (artist + album, or a pasted MusicBrainz URL).
- "Stuck in Lidarr" list: monitored albums missing for more than N days, with release date and a Pick button.
- Active jobs and the last 20 imports.

**Album page**

- Header: artist, album, release-group link, the edition Lidarr has selected (if the album is in Lidarr).
- Editions panel, collapsed by default: one row per MusicBrainz edition (date, country, label, catalog number, barcode, format, track count, disambiguation). A star pins it as the target edition.
- Candidates table, one row per Soulseek folder:

| Column | Shows |
| --- | --- |
| Match | Tier badge and the matched edition, or "one of N" with a popover listing them |
| Source | CD / WEB / Vinyl / other badge |
| Format | Bit depth and sample rate, e.g. `16/44.1`, `24/96`; a warning badge when mixed |
| Tracks | Count per disc, green when it equals the matched edition |
| Years | Original and reissue years as parsed |
| Catalog no. | Parsed token; bold when it matched an edition |
| Flags | Remaster, deluxe, SHM-CD, MFSL and the rest, as small badges |
| Sidecars | Icons for log, cue, art, scans; the log icon turns green or red once read |
| Availability | Free slot, queue length, upload speed |
| Size | Total MB |
| Actions | Fetch log, Browse folder, Download |

- Expanding a row shows the full path, the file list with each track's length next to the matched edition's length and the difference, the evidence behind every badge, and the parsed log summary when fetched.
- Search shows live progress (users and files found so far) while it runs; rows appear when slskd finishes the search, and matching fills in as editions and logs load.

**Downloads and review**

- Active transfers with progress, per candidate.
- When a download finishes, it waits in Review (unless `auto_import` is on): integrity results for every file, spectrograms of two tracks (the longest and one from the middle), the matched edition, and buttons Import, Change edition, Reject. Reject deletes the downloaded folder and offers the next-best candidate.

**History**

- Every import: date, album, edition (linked), source user, folder, outcome, and the beets output.

**Settings**

- Connection cards for slskd, Lidarr, Plex and MusicBrainz, each with a Test button.
- Paths, preferences (`prefer_original`, `auto_import`, FLAC-only, stuck-days threshold, stall timeout), and the Lidarr defaults used when adding an artist (root folder, quality profile, metadata profile, monitor option).

## External API notes

The facts below were read from each project's source in October 2026 (slskd 0.26.1, Lidarr develop 3.1.6, beets 2.14.1); the gotchas column is what will bite if ignored.

**slskd** ([source](https://github.com/slskd/slskd/tree/master/src/slskd)). Base `/api/v0`, header `X-API-Key`. The picker's key needs the ReadWrite or Administrator role (other keys default to ReadOnly).

| Call | Use | Gotchas |
| --- | --- | --- |
| `POST /searches` `{id, searchText, searchTimeout, responseLimit, fileLimit, filterResponses, minimumResponseFileCount}` | Start a search | Only one search POST at a time, else 429: serialise with a lock. `searchTimeout` (seconds, default 15) counts from the last response. |
| `GET /searches/{id}` | Poll state and counts | `state` is a flags string like `"Completed, TimedOut"`; poll until `isComplete` is true. |
| `GET /searches/{id}/responses` | Results | Filled in when the search ends, not while it runs: show live counts, then rows. |
| `POST /transfers/downloads/batches` `{username, files:[{filename,size}], options:{destination}}` | Enqueue a folder | Added in 0.26.0. `destination` is relative to the download directory. 201 all queued, 207 partial, 200 none. |
| `POST /transfers/downloads/{username}` `[{filename,size}]` | Enqueue, older servers | Marked obsolete; fall back to it only if batches return 404. |
| `GET /transfers/downloads/batches/{id}` | Poll a batch | Success = `Completed, Succeeded`; failure = `Completed` plus Cancelled, TimedOut, Errored, Rejected or Aborted. |
| `POST /users/{username}/directory` `{directory}` | List one remote folder | For Browse folder and fetching sidecars a search didn't return. |

Other notes: remote paths use backslashes; per-file `bitDepth`, `sampleRate`, `bitRate` and `length` (seconds) can be null. slskd can also call webhooks on `DownloadDirectoryComplete` instead of being polled.

**Lidarr** ([source](https://github.com/Lidarr/Lidarr/tree/develop/src/Lidarr.Api.V1)). Base `/api/v1`, header `X-Api-Key`.

| Call | Use |
| --- | --- |
| `GET /wanted/missing?monitored=true&includeArtist=true&page=&pageSize=` | Stuck queue |
| `GET /artist/lookup?term=lidarr:<mbid>` then `POST /artist` | Add artist |
| `GET /album?foreignAlbumId=<rg mbid>`, `PUT /album/{id}` | Read and select the edition (`releases[].monitored`) |
| `POST /command` `RescanFolders`, `RefreshArtist`; `GET /command/{id}` | Rescan and wait |
| Webhook `eventType: Download` | Retag trigger; no release MBID in the payload |

**MusicBrainz** ([API docs](https://musicbrainz.org/doc/MusicBrainz_API)). Base `https://musicbrainz.org/ws/2`, `fmt=json`.

| Call | Use | Gotchas |
| --- | --- | --- |
| `/release-group/?query=` | Album search |  |
| `/release?release-group=<mbid>&inc=recordings+labels+discids&limit=100&offset=` | All editions | Pages are capped by track count too; advance `offset` by the number of releases actually returned. |
| `/discid/<id>?toc=…&cdstubs=no&inc=labels+recordings+release-groups` | Disc ID match | `-` as the ID does a TOC-only lookup. |

About 1 request per second per IP; going over returns 503 for everything. Send `User-Agent: SoulseekPicker/<version> ( <contact> )`; generic agents get a lower shared limit ([rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)).

**beets** ([source](https://github.com/beetbox/beets)). Run as a subprocess with `-c <config>`; its Python API calls `sys.exit` on errors and isn't a stable interface. `-S/--search-id` pins the release; `-q` never prompts but skips weak matches; a second `-c` file overlays the first.

**Plex**. `GET http://<pms>:32400/library/sections/{id}/refresh?path=<url-encoded folder>` with `X-Plex-Token` scans one folder; the path must be the path as Plex sees it ([python-plexapi](https://github.com/pkkid/python-plexapi/blob/master/plexapi/library.py)).

## Build phases and acceptance criteria

Build in five phases; each ends with something usable and a check that proves it. Do not start a phase until the previous one passes.

| Phase | Delivers | Done when |
| --- | --- | --- |
| 1. Skeleton and search | Container, settings page with connection tests, album search via MusicBrainz, slskd search, grouping into candidates, the candidates table with raw columns, Download with progress | Searching a well-known album shows grouped FLAC folders within 30 s; a download lands in `/downloads/soulseek` intact |
| 2. Parsing | The folder parser with its fixture corpus; badges and evidence in the table | All fixtures pass; on 5 real albums, hand-checked badges are right for at least 9 of 10 folders |
| 3. Edition matching | Editions panel, matcher, tiers, ambiguity sets, `prefer_original` ordering, target-edition pin | On 5 real albums with several editions, every folder carrying a catalog number in its path is matched to that edition at Exact |
| 4. Import pipeline | Review step, integrity check, spectrograms, beets import pinned to the release, move into `/music`, Lidarr sync (add artist, set edition, rescan), Plex partial scan | A hand-picked album appears in Plexamp with the chosen edition's label, catalog number and date; Lidarr shows it as present with that edition selected and does not search for it |
| 5. Logs and Lidarr feed | Log and cue prefetch with disc ID lookup; the stuck-in-Lidarr queue; the webhook that retags Lidarr's own imports | A folder with an EAC or XLD log is matched at Exact by disc ID before download; an album Lidarr grabs on its own is retagged by beets within a minute of import without being moved |

**Cross-cutting requirements**

- Every external call has a timeout, a retry with backoff for 5xx and connection errors, and a clear error on the page that triggered it.
- MusicBrainz calls go through one rate limiter shared by the whole process.
- No destructive action on the library without an explicit click: the picker never deletes or overwrites a folder in `/music`; Reject only deletes from the download directory.
- Unit tests for the parser, matcher, log TOC parser and disc ID function; an integration test that runs the import pipeline against a fixture album in a temp directory with beets in pretend mode.
- A `README` covering setup on Unraid, the required Lidarr settings (Write Tags: Never, the webhook), and how to run the tests.
