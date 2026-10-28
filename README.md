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

**autoscan** (optional, recommended if you use it)
- Set the **autoscan** card on the Settings page (or the template's `AUTOSCAN_*` variables): URL `http://UNRAID_IP:3030`, plus the username and password from autoscan's `authentication:` block. autoscan's config needs the `manual` trigger, which a basic config has.
- After each import, and each retag of a Lidarr grab, the picker posts the album folder to `/triggers/manual?dir=…`. autoscan then queues it, applies `minimum-age` and the anchor check, and scans Plex as usual.
- **Why bother when autoscan watches with inotify:** the picker puts an album in place by *moving* the downloaded folder in one rename. A watcher sees one event for the folder and none per file, unlike a copy, which writes every file. If the artist folder was created a moment earlier, the watch on it may not exist yet. Lidarr's autoscan trigger doesn't help either: it fires only for Lidarr's own imports.
- autoscan must see the library at the same path as the picker (`/mediadata/media/music`).
- The Settings **Test** button checks the URL and login without queueing anything.

**Plex**
- No special settings, and no token needed if autoscan does the scanning. If you do give the picker a Plex token and section ID, it also asks Plex directly for a partial scan of `/mediadata/media/music/<artist>/<album>`, the path Plex already sees, so `PLEX_MUSIC_PATH` stays empty.

## Album folder names

Lidarr has no separate album folder format; the album folder is the folder part of its track format. The picker's `album_folder_template` (Settings → Import) defaults to **`{Album Title} ({Release Year})`**, the folder part of the formats above, so picker albums and Lidarr albums sit side by side with identical names. When Lidarr already has the album, its title and release year are used.

The picker renders the folder name the way Lidarr's FileNameBuilder does (`picker/naming.py`):
- `: ` becomes ` - `, `:` becomes `-`, `/` and `\` become `+`, `?` becomes `!`, `*` becomes `-`, and `< > | "` are dropped.
- Leading dots and trailing separators are trimmed, and repeated separators are collapsed.
- A missing year gives `(Unknown)`.

If you change Lidarr's formats, change the template to match; the probe flags any difference. The Settings page shows, as you type, what the template would name your last three imported albums (or an example album before any import). Existing folders are never renamed.

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

## Searching

- **Pick** (from search results, Stuck in Lidarr, or a pasted MusicBrainz link) starts the Soulseek search straight away. A search that is running or finished in the last 10 minutes is reused.
- slskd only hands out a search's results once the search has ended, and a search keeps going while responses trickle in, which can take a minute or more. So the picker stops the search once it has enough to show, then shows the folders:
  - `search_quick_seconds` (10) and `search_quick_responses` (25): stop once both have passed
  - `search_max_seconds` (45): stop in any case

  The status line says when a search was stopped early.
- **Search more** searches the same album again for up to 3 minutes and merges new folders into the table. Rows already there keep their log results and downloads.
- MusicBrainz editions load while slskd searches, instead of before.

## The recommendation card

Once a search, its matching and the automatic log fetches have settled, a card above the table names the best pick and why: the match tier and how it was verified, log and AccurateRip results, completeness and cover art, bit depth and rate, and the estimated time to obtain the folder. Its **Download** button opens the same confirm form as the row, with the preferred edition preselected; **Why not the others?** gives one short reason per other folder ("one of 3 editions; no log", "queue of 7").

Eligible folders are those at the best tier present. Ties break, in order, on verification (disc ID or a verified log, then catalog number or barcode), the edition preferences, completeness and sidecars, the time estimate, and size. The estimate is queue wait plus size over upload speed, from the response's upload speed and queue length or the speed measured on a download from that user in the last day; an unknown speed counts as slow and a free slot as no queue wait. The Availability column shows the same estimate ("~3 min", "~40 min (queue)").

## Edition preferences

Settings → Edition preferences decides which pressing wins when several folders match equally well. The match tiers are still the contract (Exact beats High whatever you prefer); within a tier the preference score orders the rows, picks the edition the confirm form preselects among a folder's possible editions, and feeds the recommendation. Every point comes with a reason, shown on the row's **pref** badge on hover ("+ GB", "+ CD", "− remaster").

| Setting | Default | Effect |
| --- | --- | --- |
| `avoid_remasters` | on | editions whose name says remaster, and folders flagged remaster, lose 10 points (3 when only some of a folder's possible editions are remasters); the confirm form warns when a remaster is selected |
| `preferred_countries` | `GB, US, XE, JP` | ordered: the first earns 4 points, the next 3, and so on; `XW` (worldwide) counts as the first; the folder's territory words are used when no edition is matched |
| `preferred_sources` | `CD, Vinyl, WEB` | ordered the same way, from the folder's source or the edition's format |
| `preferred_depth` | `any` | `16` or `24`: that bit depth earns 2 points, the other loses 2 |
| `avoid_flags` | empty | markers that lose 3 points each, e.g. `Deluxe, Anniversary`; matched against the folder's flags and territories and the editions' names and formats |
| `prefer_flags` | empty | markers that earn 3 points each, e.g. `MFSL, Japan` |

`prefer_original` (Preferences) still breaks ties after the score: earlier editions first, remaster-flagged folders last.

## On a phone

Below 720 px wide, the candidates table becomes a list of cards: match and edition, uploader and folder, source, format, tracks and the time estimate, with the Download button; tap a card for the rest (years, catalog numbers, flags, sidecars, size, and the usual detail). Sorting and filters sit in one **Sort & filter** sheet above the list. Downloads, History and the review list collapse to labelled rows, the Review page stacks its sections with spectrograms and tables scrolling inside their boxes, buttons are at least 44 px tall, and nothing scrolls sideways.

## Albums you already have

Before any bandwidth is spent, the picker checks whether the album is in the library. The album page shows a banner naming the edition and where it is, from the first source that knows: the picker's own import history (if the folder still exists), Lidarr (track files present; the monitored release is the edition), or the beets library. Find-album results carry an **in library** badge from the local sources (history and beets; Lidarr is asked on the album page only). You can still search and download; the import then stops at placing because the folder exists, since the picker never overwrites anything in the library. Replacing an album you have is the upgrades phase of the spec.

## Importing automatically

`auto_import` is on by default. A download imports straight away when it passes every check:
- the integrity check
- the spectrum check, which must find no sign of a transcode or upsampling, and must actually have run
- an edition was chosen

- the uploader's tags don't name a different edition (see below)

Anything else waits on the Review page, and the album's event log says why ("waiting for review (the spectrum check suggests a transcode)"). Turn `auto_import` off to review every download.

**Review list.** `/review` (the Review link in the top bar, with a count) lists every import waiting on you: those under review, the ones beets skipped, blocked ones, and failed checks. Each row shows the chosen edition, the checks as badges (integrity, spectrum, tag check, beets' rating) and why it waits, with Import, Force apply, Retry and Reject inline, so a batch can be cleared without opening each album. Home shows the same count.

**Tag check.** Before anything is placed or tagged, the picker reads the tags the uploader left in the files and compares them with the edition you chose when you downloaded. beets is told to ignore those tags (see the overlays under beets), so this is the only place a folder tagged as another edition gets noticed:
- **Same release**: the files already carry the chosen edition's MusicBrainz ID. Nothing to do.
- **Sister pressing**: they carry another release of the same album with the same year and kind (country, label or catalog number differ). A note on the Review page; the import goes ahead.
- **Different edition**: they name a remaster, reissue, another year, an edition with different words in its name, or a release outside this album; or there is no MusicBrainz ID but the album or comment tag says "remaster" when the chosen edition isn't one, or the date tag is another year. The import waits for review. The Review page shows what the tags say and offers **Use that edition instead** (one click: the checks run again and the import continues by itself), or press **Import** to tag the files as the edition you chose anyway.
- **Mixed**: the files name more than one release between them, which means the folder was put together from several rips. The import waits for review and the Review page lists how many tracks carry each release.
- **No MusicBrainz ID** and nothing that disagrees: the import goes ahead.

## Downloads: queue and stall timeouts

- **Queue timeout** (default 6 h): time allowed waiting in an uploader's queue. The Downloads page shows the live queue position and how long it has waited.
- **Stall timeout** (default 30 min): counts only once bytes have started flowing and then stop.
- **Give up and try next candidate**: stops the current folder now and starts the next-best one for the same edition.

**Transcode check.** A FLAC made from an MP3 or AAC decodes perfectly, so `flac -t` can't catch it. Lossy encoders cut everything above a low-pass frequency (around 16 kHz at 128 kbps, 19 kHz at 192, about 20 kHz at 256–320), and that shows up as a cliff in the spectrum, whereas real recordings fade out gradually.

Before Review, the picker analyses every track (three ffmpeg decodes at a time). It decodes three 10-second stretches of each and checks where the spectrum ends:
- **Possible transcode**: a cliff, reported with its frequency and what bitrate it's consistent with.
- **Possibly upsampled**: a 96 kHz file with nothing above about 22 kHz.
- **Full-range**: no cliff.

Steep drops above about 21 kHz on CD audio are the recording's own anti-alias filter and count as full-range. Some masterings are genuinely band-limited (old tape, some early digital), so this is a warning on the Review page and in History, never a reject. The spectrograms sit right below it for a second look. It can be turned off with `transcode_check`, and `cutoff_db` sets how sharp a cliff must be (default 30 dB).

**Rip verification.** `flac -t` proves a file decodes, not that it holds the right audio. For CD-audio folders (16-bit 44.1 kHz stereo) the picker checks the decoded tracks against the two databases of real CD rips, before Review:
- The disc's table of contents comes from the EAC/XLD log in the folder when its track lengths fit the files, otherwise from the track lengths rounded up to whole sectors (the way CUETools does it). Discs with hidden tracks or dropped gaps won't be found that way, and that is reported as "not in database".
- **CUETools DB** (primary): one lookup per disc with the TOC, then the CRC32 of the disc's audio (minus 5880 samples at the start and the stride remainder at the end) and one per track, compared with the entries for this pressing. *Verified (confidence N)* when the disc CRC matches.
- **AccurateRip** (cross-check): the v1 and v2 track checksums (track 1 skips its first 2939 samples, the last track its last 2940), compared per track with the `dBAR` record for the disc IDs; the frame-450 sums find a pressing offset, and the checksums are recomputed at that offset. The AccurateRip owners say third-party access needs their agreement; whipper and CUETools query it the same way, and `accuraterip` turns it off (CUETools DB stays on).
- The formulas were taken from the whipper and CUETools sources, which agree, and checked against live lookups. Lookups are cached for a day. Memory stays low: tracks are decoded with ffmpeg one at a time.

The Review page shows the verdict (verified, not in database, no match, or which tracks differ) with a per-track table when something is off, and History carries a badge. Verification is a signal, not a gate: no match is common for obscure pressings and for vinyl or WEB sources. The one exception holds an import for review: a track that differs while its siblings verify, which means that file's audio is wrong even though it decodes. The Review page then offers **Re-download the differing tracks**, which deletes just those and fetches them again from the same uploader. `rip_verify` turns the check off.

**Damaged files.** Soulseek has no file hashes, so a transfer damaged on the way can still finish as "Completed, Succeeded". In one real case, a burst of 44 garbled bytes in the middle of a track got past TCP's checksum somewhere between the uploader and the VPN exit. The import's integrity check (`flac -t`, plus a mutagen read) is what catches this.

When files fail it, the picker deletes the damaged copies and downloads just those files again, once, automatically. It then checks the whole folder again. If they fail a second time, the Review page offers:
- **Re-download failed tracks**
- **Retry**, which re-checks the folder, e.g. after you replaced a file by hand
- **Reject and try the next folder**

Only files inside the download directory's `picker/` folder are ever deleted.

## Database backups

The picker's SQLite database (`/config/picker.db`) holds the import history, which is the only record of which edition each folder was tagged as. Every container start copies it to `/config/backups/picker-<timestamp>.db` before opening it, using SQLite's online backup, and keeps the newest 10 copies. To restore one, stop the container and copy it over `picker.db` (delete `picker.db-wal` and `picker.db-shm` if present).

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
- **Tags the picker overwrites don't count.** Every import passes `/config/beets/pinned.yaml`, which gives zero weight to album-level tags already in the files: another pressing's MusicBrainz IDs, country, label, catalog number, year and media. The picker has already chosen the release, so beets only judges whether the files are its tracks (count, lengths, titles, artist, album). Without this, files tagged for the Australian pressing of an album scored 82.7% against the identical UK release and were skipped.
- **Why beets skipped it:** beets itself only prints "Skipping." When it does, the picker runs beets' matcher once more in a helper process (`picker/beets_explain.py`, same config and overlay, pinned to the same release) and shows its distance breakdown on the Review page: the rating against the threshold, which album-level fields cost points, and for every file that didn't pair cleanly the track it was paired with, both lengths, and whether the title or length differed; plus tracks of the release with no file and files matching no track. That is what to read before deciding on Force apply.
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

## The current specification

`SPEC.md` describes the app as built: every subsystem's rules, the data model, the pipeline, the pages, and what is not built. The older `soulseek-release-picker-spec.md` (v1 request) and `soulseek-release-picker-spec-v2.md` (v2 roadmap) are kept for history.

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
