# Soulseek release picker

A self-hosted web app for choosing the exact edition of an album to download from Soulseek. beets tags the download to that edition in place, Lidarr is told which edition it is, and Plex rescans the folder. Lidarr keeps doing automatic grabs; the picker never downloads without a click. The design is in [soulseek-release-picker-spec.md](soulseek-release-picker-spec.md).

Image: **`ghcr.io/bambinofresco/soulseek-picker`**. Tags:
- `latest` and the short commit SHA, on every push to `main`
- `1.2.3` for a `v1.2.3` tag

It's built for linux/amd64 by GitHub Actions, and only after the tests pass.

## Install on Unraid

The picker runs on Docker's **bridge** network next to Lidarr and Gluetun/slskd. Container names don't resolve on the bridge network, so every service is reached by the server's LAN IP.

1. Get the template onto the server:
   ```sh
   wget -O /boot/config/plugins/dockerMan/templates-user/my-soulseek-picker.xml \
     https://raw.githubusercontent.com/bambinofresco/soulseek-picker/main/unraid/picker.xml
   ```
   (Or copy [unraid/picker.xml](unraid/picker.xml) there by hand.)
2. **Docker → Add Container → Template:** pick **soulseek-picker** under *User templates*.
3. **Replace `UNRAID_IP`** in the slskd, Lidarr and Plex URLs with the server's LAN IP, e.g. `http://192.168.1.10:5030`. Until you do, every page shows a warning.
4. Check the pre-filled paths and press **Apply**:

   | Container | Host | Notes |
   | --- | --- | --- |
   | `/mediadata` | `/mnt/user/mediadata` | Same mapping as slskd and Lidarr |
   | `/config` | `/mnt/user/appdata/picker` | Database, settings, beets config and library, spectrograms, probe output |
   | port 8765 | 8765 | Web UI and Lidarr's webhook target |

   Inside `/mediadata`, the defaults are:
   - slskd download dir `/mediadata/soulseek/complete`
   - library `/mediadata/media/music` (also Lidarr's root folder)

   Files are written as `PUID`/`PGID` 99/100.
5. Open `http://UNRAID_IP:8765/settings`. Fill in any API keys or tokens you left empty in the template, press **Test** on each card, then press **Run probe** (see [Checking the real APIs](#checking-the-real-apis)).

API keys and passwords can go in the template or on the Settings page. A value set in the template is read-only on the Settings page; an empty template variable leaves the field editable there.

## Settings in the other apps

**Lidarr**
- **Settings → Metadata → Write Audio Tags: Never.** beets is the only tag writer. The probe checks this.
- **Settings → Connect → + → Webhook**:
  - URL `http://UNRAID_IP:8765/hooks/lidarr`, method POST
  - **On Release Import** and **On Upgrade** ticked
  - username and password matching the template's Webhook username and password

  Press **Test**: the picker answers 200 and saves the payload for the probe.
- **Settings → Media Management → Rename Tracks: on**, with these formats (the picker's defaults assume them):
  - Standard: `{Album Title} ({Release Year})/{track:00} - {Track Title}`
  - Multi-disc: `{Album Title} ({Release Year})/{medium:0}{track:00} - {Track Title}`
  - Artist folder: `{Artist Name}`

**slskd**
- An API key for the picker.
- `remote_file_management: true` is fine; see [Coexisting with Lidarr's slskd plugin](#coexisting-with-lidarrs-slskd-plugin).

**Plex**
- No special settings. The partial scan uses `/mediadata/media/music/<artist>/<album>`, the path Plex already sees, so `PLEX_MUSIC_PATH` stays empty.

## Album folder names

Lidarr has no separate album folder format; the album folder is the folder part of its track format. The picker's `album_folder_template` (Settings → Import) defaults to **`{Album Title} ({Release Year})`**, the folder part of the formats above, so picker albums and Lidarr albums sit side by side with identical names. When Lidarr already has the album, its title and release year are used.

The picker renders the folder name the way Lidarr's FileNameBuilder does (`picker/naming.py`):
- `: ` becomes ` - `, `:` becomes `-`, `/` and `\` become `+`, `?` becomes `!`, `*` becomes `-`, and `< > | "` are dropped.
- Leading dots and trailing separators are trimmed, and repeated separators are collapsed.
- A missing year gives `(Unknown)`.

If you change Lidarr's formats, change the template to match; the probe flags any difference.

Tokens:

| Token | Value |
| --- | --- |
| `{Album Title}`, `{Release Year}`, `{Artist Name}`, `{Album Type}`, `{Album Disambiguation}`, `{Album MbId}`, `{Artist MbId}` | As in Lidarr |
| `{Catalog Number}`, `{Label}`, `{Country}`, `{Media}`, `{Edition Date}`, `{Edition Year}` | Picker extras, from the chosen edition (Lidarr can't render these) |

As in Lidarr, text inside the braces around a token disappears with it when the token is empty. `{Album Title} ({Release Year}){ [Catalog Number]}` gives `OK Computer (1997) [CDP 7243 8 55229 2 5]`, or `OK Computer (1997)` when there's no catalog number. The extras aren't used by default. If you use them, picker albums will be named differently from Lidarr's own grabs of the same album.

## Lidarr's own grabs

When Lidarr imports an album itself, it renames the files first and then fires On Release Import. The payload's `trackFiles[].path` holds the **final** paths in the artist folder; Lidarr's source confirms it builds the payload after the move.

The picker then:
1. waits 60 s, debounced per album, so a multi-disc import fired twice is retagged once
2. takes the folder holding those files and the album's single monitored release
3. runs beets **in place**, pinned to that release

Nothing is moved, and Lidarr's edition choice is left alone. Paths outside the music library are refused.

## Coexisting with Lidarr's slskd plugin

I read [Lidarr.Plugin.Slskd](https://github.com/allquiet-hub/Lidarr.Plugin.Slskd) 1.1.6.0. It only treats a transfer as its own when the transfer belongs to an slskd **batch whose destination starts with `lidarr/`**. Transfers with no batch, or a batch outside `lidarr/`, are skipped as "enqueued outside of Lidarr". Its cancels and deletes are limited to its own transfers and the `lidarr/<id>` folder, and it never bulk-clears completed transfers.

The picker's downloads go to `picker/<job>/` (one batch per disc folder) and its log/cue prefetches to `picker/prefetch/<id>/`. The plugin never sees or touches them, and the code refuses any picker destination under `lidarr/`.

The one interaction: slskd refuses to enqueue a file that's already in flight from the same user. If Lidarr and the picker grab the same folder from the same user at the same moment, the second enqueue fails. The picker reports it on the album's event log.

## Downloads: queue and stall timeouts

- **Queue timeout** (default 6 h): time allowed waiting in an uploader's queue. The Downloads page shows the live queue position and how long it has waited.
- **Stall timeout** (default 30 min): counts only once bytes have started flowing and then stop.
- **Give up and try next candidate**: stops the current folder now and starts the next-best one for the same edition.

**Damaged files.** Soulseek has no file hashes, so a transfer damaged on the way can still finish as "Completed, Succeeded". In one real case, a burst of 44 garbled bytes in the middle of a track got past TCP's checksum somewhere between the uploader and the VPN exit. The import's integrity check (`flac -t`, plus a mutagen read) is what catches this.

When files fail it, the picker deletes the damaged copies and downloads just those files again, once, automatically. It then checks the whole folder again. If they fail a second time, the Review page offers:
- **Re-download failed tracks**
- **Retry**, which re-checks the folder, e.g. after you replaced a file by hand
- **Reject and try the next folder**

Only files inside the download directory's `picker/` folder are ever deleted.

## Checking the real APIs

The probe runs only read-style calls against slskd, Lidarr and Plex, plus one small Soulseek search that it deletes afterwards. It saves the raw JSON, with keys, tokens and passwords scrubbed, to `/config/probe/<timestamp>/`, then compares it with the fields the code reads:

```sh
docker exec soulseek-picker python -m picker probe
```

or press **Run probe** on the Settings page. The report (also in `report.txt`) marks each item ok, mismatch, warn or error. It covers:
- **API shapes**: search, transfer and batch replies (including the batch download reply), queue position, `wanted/missing`, `releases[]`, commands, Plex sections.
- **Configuration**:
  - slskd's download dir vs `DOWNLOAD_DIR`
  - Write Audio Tags is `no`
  - Lidarr's album folder format vs the picker's template
  - the root folder
  - the Plex library path
- **Webhook payloads**: every payload Lidarr sends is kept under `/config/probe/webhooks/`. Press Test in Lidarr, or let it import something, then probe again.

Paste the report back to whoever is debugging; it has no secrets in it.

## Acceptance checks

Run these on the real stack after installing:

1. **Search and download:** searching a well-known album shows grouped FLAC folders within 30 s, and a download lands under `/mediadata/soulseek/complete/picker/<job>/` intact (Downloads page, then the Review page's integrity table).
2. **Badges:** on 5 real albums, hand-checked badges (year, catalog number, source, flags) are right for at least 9 of 10 folders. Hover a badge to see the evidence.
3. **Matching:** on 5 real albums with several editions, every folder carrying a catalog number in its path is matched to that edition at **Exact**.
4. **Import:** a hand-picked album, imported from the Review page:
   - **History → tags** shows the chosen edition's LABEL, CATALOGNUMBER, DATE/ORIGINALDATE and MUSICBRAINZ_ALBUMID, read from the files on disk, with the badge *tags match the chosen edition*.
   - Lidarr shows the album as present with that edition selected, and doesn't search for it.
   - The folder is `/mediadata/media/music/<Artist>/<Album> (<Year>)`.
5. **Logs and Lidarr's grabs:**
   - A folder with an EAC or XLD log is matched at Exact by disc ID before download (log icon green, "disc ID matches" in the expanded row).
   - An album Lidarr grabs on its own appears in History within about a minute of import, retagged by beets, with its path unchanged.

## Where this build differs from the spec

Found by reading the tools' source or running them:

- **beets `plugins` line:** in beets 2.x the MusicBrainz autotagger is a plugin, on by default. The spec's `plugins: fetchart` replaces the default list, so beets finds no candidates and skips every import. The generated config says `plugins: musicbrainz fetchart`, and the Settings page warns if `musicbrainz` goes missing.
- **Force apply:** beets applies a match only when `distance < strong_rec_thresh` (strictly, with distances from 0 to 1), so the spec's `1.0` still skips the worst case. Also, `max_rec` caps matches with missing or unmatched tracks at medium. The overlay uses `strong_rec_thresh: 1.01` and lifts those two caps. `-c` takes one file; the base config comes from `BEETSDIR=/config/beets`.
- **slskd 0.26.1:**
  - `searchTimeout` is in milliseconds (the setting stays in seconds).
  - Batch files land flat in the destination, so the picker sends one batch per disc folder; otherwise `CD1/01.flac` and `CD2/01.flac` would collide.
  - A batch 404 means the uploader is offline; batch support is detected from slskd's version.
- **Lidarr:**
  - The webhook's `eventType` is matched case-insensitively (Lidarr sends `download`).
  - `RefreshArtist` uses `artistIds`.
- **Matching:** remaster mismatches lower an edition's score but don't cap the tier at Low.
- **Rip quality** is scored out of 4 (log, secure ripper, AccurateRip/checksum, cue) and completeness out of 2 (all tracks, cover art).

## Development

Needs Python 3.12.

```sh
uv venv --python 3.12 .venv
uv pip install -p .venv -e ".[dev]"
.venv/Scripts/python -m pytest          # .venv/bin/python on Linux/macOS
```

The tests never touch the network.
- The import pipeline test runs real beets in `--pretend` mode against a generated album.
- The rip-log tests compare disc IDs with MusicBrainz's own records, and with libdiscid when it's installed (it is in CI).
- The probe tests run against mock services shaped like slskd 0.26.1, Lidarr and Plex.

If the parser misreads a real folder name, add it to `tests/fixtures/folder_names.yaml` with the parse it should have produced, then fix the pattern tables until the corpus passes. Any field a case doesn't list must come out empty.

To release: push to `main` for `latest`, or tag `vX.Y.Z` for a versioned image.

### Layout

| Module | Does |
| --- | --- |
| `picker/config.py` | Settings: `/config/picker.yaml`, overridden by non-empty environment variables |
| `picker/models.py`, `db.py` | SQLite tables; new columns are added on start |
| `picker/jobs.py` | In-process job runner; resumes after a restart; `run_after` for debounced jobs |
| `picker/clients/` | slskd, MusicBrainz (shared 1 req/s limiter), Lidarr, Plex; timeouts and retry with backoff |
| `picker/queries.py`, `grouping.py`, `parsing.py` | Search text; responses → candidate folders; folder names → hints with evidence |
| `picker/editions.py`, `matching.py` | Editions; scoring, tiers, ambiguity sets |
| `picker/riplog.py`, `prefetching.py` | Rip-log TOCs → disc IDs, cue CATALOG/ISRC; the prefetch job |
| `picker/searching.py`, `downloading.py` | Search and download jobs; queue and stall timeouts |
| `picker/naming.py` | Album folder names, Lidarr-compatible |
| `picker/audio.py`, `beets.py`, `importing.py` | Integrity, spectrograms, tag snapshot; the beets subprocess; the import pipeline |
| `picker/lidarr_feed.py` | Stuck-wanted queue and webhook retags |
| `picker/probe.py` | The API probe |
| `picker/web.py`, `templates/` | Pages and HTMX partials |
| `unraid/picker.xml`, `.github/workflows/image.yml` | Unraid template; test-then-publish workflow |
