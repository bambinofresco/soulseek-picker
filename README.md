# Soulseek release picker

A self-hosted web app for choosing the exact edition of an album to download from Soulseek. It tags the download to that edition with beets and files it into the library, keeping Lidarr and Plex in step. The full design is in [soulseek-release-picker-spec.md](soulseek-release-picker-spec.md).

## Status

All five phases from the spec are built and covered by tests. Each phase's acceptance check needs the real stack (slskd, Lidarr, Plex), so none of them has been run yet; see [Acceptance checks still to run](#acceptance-checks-still-to-run).

| Phase | What's there |
| --- | --- |
| 1. Skeleton and search | Settings with connection tests, MusicBrainz album search (or a pasted URL), slskd search with fallback queries, grouping into candidate folders, Download with progress, stall timeout and "try next candidate" |
| 2. Parsing | Folder-name parser (`picker/parsing.py`) with a 124-case fixture corpus; Source, Years, Catalog no. and Flags columns, each badge showing its evidence on hover; sort by column; filter by source and bit depth |
| 3. Edition matching | Editions panel, matcher with tiers and "one of N" ambiguity sets, `prefer_original` ordering, target-edition pin, confirm-edition step before downloading |
| 4. Import pipeline | Integrity check, spectrograms, review page (Import / Change edition / Reject), add the artist to Lidarr, move the folder into place, beets import pinned to the release (Force apply when beets skips), select the edition in Lidarr and rescan, Plex partial scan, History |
| 5. Logs and Lidarr feed | Rip-log and cue prefetch with MusicBrainz disc ID lookup, "Stuck in Lidarr" queue, webhook that retags Lidarr's own imports in place |

## Setup on Unraid

1. Build the image on the server (`docker build -t soulseek-picker .` in this folder), or push it to a registry you use.
2. Add the container with [docker/unraid-template.xml](docker/unraid-template.xml) (copy it to `/boot/config/plugins/dockerMan/templates-user/`), or use [docker-compose.example.yml](docker-compose.example.yml) as a guide.
3. Put it on the same Docker network as Lidarr and Gluetun. slskd runs inside Gluetun's network stack, so the picker reaches it at `http://gluetun:5030`.
4. Mount the same host folders at the same container paths in every container:
   - `/downloads/soulseek` is slskd's download directory. It must be the same path inside slskd and the picker, and on the same filesystem as `/music`, so placing an album is a rename.
   - `/music` is the library root, at the same path as inside Lidarr. If Plex sees it under another path, set `plex_music_path`.
   - `/config` holds the picker's database, settings, beets config and library, and spectrograms.
5. Open `http://<server>:8765/settings` and press **Test** on each connection card.
   - The slskd test warns when slskd's download directory isn't visible to the picker.
   - The Lidarr test lists root folders and profile IDs for the "new artist" defaults.

The container runs as `PUID`/`PGID` (default `99:100`, `nobody:users`).

### Configuration

The Settings page writes `/config/picker.yaml`. Environment variables override it field by field, using the field name in upper case (`SLSKD_API_KEY`, `LIDARR_URL`, `MB_CONTACT`, `DOWNLOAD_DIR`, …). Fields set by the environment are read-only on the Settings page, and secrets are never shown back.

Required:
- `SLSKD_URL` and `SLSKD_API_KEY` (a key with the ReadWrite or Administrator role)
- `LIDARR_URL` and `LIDARR_API_KEY`
- `PLEX_URL`, `PLEX_TOKEN` and `PLEX_SECTION_ID`
- `MB_CONTACT` (an email or URL for the MusicBrainz User-Agent)

For the webhook, also set `WEBHOOK_PASSWORD`.

### Required Lidarr settings

- **Settings → Media Management → Write Audio Tags: Never.** beets is the only thing that writes tags.
- **Settings → Connect → + → Webhook**:
  - URL `http://picker:8765/hooks/lidarr`, method POST
  - triggers **On Release Import** and **On Upgrade**
  - username and password matching `webhook_username` / `webhook_password`

  The picker refuses webhook calls until a password is set. Lidarr's Test button should get a 200.
- Album folder format: set the picker's `album_folder_template` (default `{album} ({year})`) to produce the same folder names.

### beets

On first start the picker writes `/config/beets/config.yaml` from the spec's template, then leaves it alone; it's yours to edit. beets runs as a subprocess with `BEETSDIR=/config/beets`. Force apply passes `-c /config/beets/force.yaml`, which beets layers on top of that base config.

The Settings page warns if the beets config stops loading the `musicbrainz` plugin, or if it turns on `move` or `copy`.

## Where this build differs from the spec

These were found by reading the installed tools' source or by running them, and are deliberate:

- **beets `plugins` line.** The spec's config has `plugins: fetchart`. In beets 2.x the MusicBrainz autotagger is itself a plugin that is on by default, and a `plugins` line replaces the default list. With the spec's line, beets finds **no candidates** and skips every import. The generated config says `plugins: musicbrainz fetchart`.
- **Force apply.** beets applies a match only when `distance < strong_rec_thresh` (strictly), and distances run from 0 to 1. The spec's `1.0` therefore still skips the worst case. Also, `max_rec` caps matches with missing or unmatched tracks at medium, which quiet mode skips too. The overlay uses `strong_rec_thresh: 1.01` and lifts those two caps.
- **Second `-c`.** In beets 2.14.1 `-c` takes a single file (the last one wins); it doesn't stack. The base config comes from `BEETSDIR` instead, and the overlay is the one `-c`.
- **Remaster mismatches lower the score instead of capping the tier at Low.** They're how an original and its remaster are told apart. The contradictions that cap at Low are a source/format clash and bad durations.
- **Rip quality is scored out of 4** (log, secure ripper, AccurateRip or checksum, cue). Completeness is scored out of 2 (all tracks, cover art). Both show in the Sidecars column.

## Things to know

- **Stall timeout counts queue time.** As the spec says, 30 minutes without progress fails a transfer, even when the user is just a long way down their upload queue.
- **Log prefetch needs slskd 0.26 or newer**, because it uses the batch API to put the files in their own folder. Album downloads fall back to the older per-file call. Files then land in slskd's default folder, and the import pipeline reads them from there.
- **Lidarr debounce.** Webhook retags wait 60 seconds after the last event for an album. The wait is stored in the jobs table, so a restart doesn't lose it.
- **Spectrograms** come from sox. The page notes that a lossy transcode usually shows a hard shelf around 16 or 19 kHz, but judging that is up to you.

## Acceptance checks still to run

These need the real stack, so they haven't been run yet:

1. Searching a well-known album shows grouped FLAC folders within 30 s, and a download lands in `/downloads/soulseek` intact.
2. On 5 real albums, hand-checked badges are right for at least 9 of 10 folders.
3. On 5 real albums with several editions, every folder carrying a catalog number in its path is matched to that edition at Exact.
4. A hand-picked album appears in Plexamp with the chosen edition's label, catalog number and date. Lidarr shows it as present with that edition selected, and doesn't search for it.
5. A folder with an EAC or XLD log is matched at Exact by disc ID before download. An album Lidarr grabs on its own is retagged by beets within a minute of import, without being moved.

What has been run locally, against live MusicBrainz, a fake slskd and real beets:
- search, automatic log prefetch, and disc ID matching by real disc ID
- download, verify, review, place
- beets skip, Force apply, and tags written for the chosen edition (label, catalog number, date, barcode, release IDs)

## Development

Needs Python 3.12.

```sh
uv venv --python 3.12 .venv
uv pip install -p .venv -e ".[dev]"
.venv/Scripts/python -m pytest          # .venv/bin/python on Linux/macOS
```

The tests never touch the network. The import pipeline test runs real beets in `--pretend` mode against a generated album in a temp directory. The rip-log tests compare disc IDs with MusicBrainz's own records (the fixtures are rebuilt by `tests/make_log_fixtures.py`). They also cross-check against libdiscid when it's installed.

To run the app locally, point the paths somewhere writable:

```sh
CONFIG_DIR=./dev/config DOWNLOAD_DIR=./dev/downloads MUSIC_DIR=./dev/music PORT=8765 \
  .venv/Scripts/python -m picker
```

If the parser misreads a real folder name, add the name to `tests/fixtures/folder_names.yaml` with the parse it should have produced, then fix the pattern tables until the corpus passes again. Any field a case doesn't list must come out empty, so a false positive fails the test too.

### Layout

| Module | Does |
| --- | --- |
| `picker/config.py` | Settings: YAML file plus env overrides |
| `picker/models.py`, `db.py` | SQLite tables (jobs, searches, candidates, downloads, imports, cache); new columns are added on start |
| `picker/jobs.py` | In-process job runner over the `jobs` table; resumes after a restart; `run_after` for debounced jobs |
| `picker/clients/` | slskd, MusicBrainz (one shared 1 req/s limiter), Lidarr, Plex; timeouts and retry with backoff |
| `picker/queries.py` | Soulseek query text: normalisation and fallbacks |
| `picker/grouping.py` | Search responses → candidate folders (multi-disc merge, sidecars, default filter) |
| `picker/parsing.py` | Folder names → years, catalog numbers, barcodes, label, source, quality, remaster and edition flags, territory, rip tool, each with its evidence |
| `picker/editions.py`, `matching.py` | MusicBrainz releases → editions; scoring, tiers, ambiguity sets (weights in one table) |
| `picker/riplog.py`, `prefetching.py` | EAC/XLD log TOCs → disc IDs, log quality, cue CATALOG/ISRC; the prefetch job |
| `picker/searching.py`, `downloading.py` | The search and download jobs, candidate ordering |
| `picker/audio.py`, `beets.py`, `importing.py` | Integrity and spectrograms; the beets subprocess; the import pipeline |
| `picker/lidarr_feed.py` | Stuck-wanted queue and webhook retags |
| `picker/web.py`, `templates/` | Pages and HTMX partials |
