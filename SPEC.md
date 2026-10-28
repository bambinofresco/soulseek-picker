# Soulseek release picker: current specification

What the app is and how it works, as built on 28 October 2026. This describes the code in this
repository, not a plan. The earlier documents remain for history: `soulseek-release-picker-spec.md`
(the v1 request), `soulseek-release-picker-spec-v2.md` (the v2 roadmap, now mostly built) and
`README.md` (installation and operation). Where this document and the code disagree, the code wins
and this document needs fixing.

## 1. Purpose

A self-hosted web app for one person who wants specific pressings of albums in FLAC. It searches
Soulseek through slskd for an album, works out which MusicBrainz edition each uploader's folder
is, lets the user pick one folder and one edition, downloads it, checks the files, tags them with
beets pinned to that edition, files the folder into the music library the way Lidarr would, and
keeps Lidarr and Plex in step. Lidarr stays the library manager; the picker is for the albums
Lidarr can't find or where the edition matters.

Guarantees the whole design keeps:

- Nothing under the music library is ever deleted or overwritten. A folder that already exists
  blocks placing and the user decides.
- Downloads are never started without a click. Searching, log fetching, matching and checking
  may be automatic; downloading is not.
- beets is the only program that writes tags.
- Secrets never appear in logs, pages or probe output. The repository holds placeholders only.

## 2. Shape of the system

- **Runtime:** Python 3.12, FastAPI, Jinja2 templates, HTMX 2.0.3 (vendored) plus one small
  script for the candidates table, SQLModel on SQLite (WAL, timezone-aware datetimes). One
  process, one container (`ghcr.io/bambinofresco/soulseek-picker`, built by GitHub Actions on
  every push to `main`, tests gating the build). An Unraid template is in `unraid/`.
- **Services it talks to:** slskd 0.26.1 (search, download, transfer states), MusicBrainz (release
  groups, releases, disc IDs; 1 request/s), Lidarr (artists, albums, releases, commands,
  webhook), Plex (partial scans), autoscan (manual trigger), CUETools DB and AccurateRip (rip
  checksums), beets 2.14.1 as a subprocess, ffmpeg, flac, sox, mutagen, numpy.
- **Storage:** `/config/picker.db` (SQLite), `/config/picker.yaml` (settings), `/config/beets/`
  (beets config, library, overlays), `/config/spectrograms/`, `/config/probe/`,
  `/config/backups/` (database copies). One `/mediadata` volume shared with slskd and Lidarr:
  slskd downloads under `/mediadata/soulseek/complete`, the library under
  `/mediadata/media/music`.
- **Jobs:** an in-process runner over a `jobs` table. Every queued job is its own asyncio task;
  jobs left running by a restart are queued again, so every handler is safe to re-run. Job kinds:
  search, prefetch (logs), download, import, stuck-refresh, retag, index (the library index).
- **Settings:** `picker.yaml`, each field overridable by an environment variable of the same
  name in upper case. The Settings page saves only values that differ from the defaults, so a
  later change of default reaches existing installs.

## 3. Data model

| Table | What one row is |
| --- | --- |
| `album` | A MusicBrainz release group the user looked at: artist, title, first release date, type, smallest edition's track count, optional pinned target edition. |
| `search` | One Soulseek search for an album: queries tried, state, counts, whether it stopped early, whether it was the longer "Search more". |
| `candidate` | One uploader's album folder from a search: files, audio format, bit depth and rate, disc layout, sidecars (log, cue, images, scans), free slot, queue length, upload speed, the parsed hints, the match result, fetched log info. |
| `download` | One download of a candidate as one edition: slskd destination, per-file states, bytes, chunking window, queue position, timing. |
| `import` | One trip through the import pipeline (also the History row): step, state, every check's result (integrity, spectrograms, spectrum, tag check, rip check), beets output and match breakdown, final folder, notes. |
| `job`, `jobevent` | The job queue and the per-album event log. |
| `libraryfile` | One audio file under the music directory as the library index last saw it: path, size, mtime, duration, bit depth and rate, identifying tags (artist, album artist, album, title, track and disc number), MusicBrainz release, release-group and recording IDs, Lidarr's track-file ID when Lidarr lists the path. |
| `cacheentry` | Cached MusicBrainz, beets-library and rip-database responses with a fetch time; the last index pass's summary. |

## 4. Finding an album

The home page has an artist field and an album field, or takes a pasted MusicBrainz URL.
Results are MusicBrainz release groups; each carries an **in library** badge when the picker's
import history, the beets library or the library index already holds it. The album page asks
Lidarr as well. The **library index** (`picker/library.py`) is one `libraryfile` row per audio
file under the music directory, read with mutagen and never written: refreshed for the folder
an import or retag touched, nightly at 03:00 local for the whole library (files with the same
size and mtime are not re-read; rows for files that are gone are removed), and on demand. A
full pass with Lidarr configured also compares the index with Lidarr's track files and keeps
the differences for the Library page. **Pick** opens the album page and starts a
Soulseek search at once unless one is running or finished in the last 10 minutes.

The home page also lists **Stuck in Lidarr**: monitored albums Lidarr has wanted for longer than
`stuck_days` (default 3), refreshed every hour and on demand, each with a Pick link.

## 5. Searching Soulseek

- Queries are built from the artist and album names: diacritics folded, bracketed suffixes and
  punctuation dropped, a leading "the" removed. The primary query is artist plus album; when it
  finds fewer than `min_flac_folders` FLAC folders, up to two broader queries follow (the album
  title alone when distinctive, then artist plus the album's first strong word).
- slskd only returns responses once a search has ended, so the picker stops a search early:
  after `search_quick_seconds` (10) once `search_quick_responses` (25) users have answered, and
  in any case at `search_max_seconds` (45). **Search more** runs the same queries for up to 3
  minutes and merges new folders into the existing table.
- Responses are grouped into candidates: one per user per album folder, with disc subfolders
  kept together. A multi-folder response is split by folder. Each candidate records its audio
  format, counts per disc, total size, sidecars and the user's availability.
- The default filter shows FLAC only (`flac_only`), at least 3 audio files, and at least half the
  smallest edition's track count. **Show all** lifts it.

## 6. Reading a folder name (parsing)

`parse_candidate` is a pure function over the remote path segments from the share root to the
album folder. The artist name and album title are blanked out first so an album called "Deluxe"
or an artist called "Japan" is not read as an edition marker. It reports, each with the evidence
it came from (token, segment, meaning, shown on hover in the table):

original year and reissue year; catalog numbers and barcodes; label; source (CD, Vinyl, WEB with
store, SACD, DVD-A, Blu-ray, Cassette) and bit depth and sample rate when the path says so;
remaster (strong from the folder name, weak from track names); flags (Deluxe, Expanded,
Anniversary, Bonus, Reissue, SHM-CD, Blu-spec, MFSL, DCC, Gold, First press, Promo, OOP, Mono,
Stereo); territories (JP, GB, US, DE, ...); the rip tool named in sidecar files.

## 7. Editions and matching

The album's editions are MusicBrainz's releases in the release group (cached 7 days), reduced
to: date, country, status, labels and catalog numbers, barcode, formats per medium, track counts
and lengths per medium, disc IDs per medium. Video media are ignored for counting.

`match_candidate` scores a folder against every edition and returns a **tier** plus the
**ambiguity set**: the editions tied at the top score. Editions that share a track list and
lengths stay together as "one of N" until a signal tells them apart; the picker never forces a
single guess. Signals and weights (a starting point, tuned against real folders):

| Signal | Weight | Meaning |
| --- | --- | --- |
| disc_id | +100 | a fetched rip log's TOC gives a disc ID this edition carries |
| catalog, barcode | +40 | a parsed catalog number or barcode matches |
| tracks / tracks_mismatch | +10 / −20 | track counts per disc equal the edition's |
| durations / partial / bad | +20 / +5 / −10 | track lengths within 2 s of the edition's |
| year / year_miss | +8 / −3 | the path's year equals the edition's |
| label / label_miss | +8 / −3 | |
| source / source_conflict | +8 / −25 | the path's source matches the edition's format |
| remaster / conflict / unflagged | +6 / −6 / −3 | remaster words in the path versus the edition's name |
| markers | +4 | Deluxe, Mono, MFSL and so on, both sides |
| territory / territory_miss | +3 / −2 | |

Tiers: **Exact** when a disc ID matches, or a catalog number or barcode matches without a
contradiction; **Low** when the track count disagrees or the source or durations contradict
(this caps even a catalog match, because labels reuse one number across formats); **High** when
durations agree together with year, label or source; **Medium** for any other positive signal;
otherwise Low or Unknown.

The Editions panel on the album page lists every edition; pinning one as the **target** filters
the table to folders whose ambiguity set includes it.

## 8. Rip logs before downloading (prefetch)

For candidates with a free upload slot, the `.log` and `.cue` files are fetched automatically
into `picker/prefetch/<candidate>/` (others get a **Fetch log** button). An EAC or XLD log's TOC
yields a MusicBrainz disc ID (data tracks on enhanced CDs detected by the 11,400-sector gap and
dropped); releases carrying it within this release group become the disc_id signal. A cue's
CATALOG line is a barcode signal. The log also gives rip quality: ripper, read mode, AccurateRip
and CRC results, errors, checksum presence. Results are cached by user and remote folder.

## 9. Ordering, preferences and the recommendation

Within a tier, rows are ordered by the **edition preference score**, then `prefer_original`
(earlier editions first, remaster-flagged folders last), then availability (free slot, shortest
queue, fastest upload). Column headers sort instead, keeping that order for ties.

Preferences (Settings → Edition preferences), each point shown with its reason on the row's
**pref** badge: `avoid_remasters` (−10 for a remaster edition or folder, −3 when only some of the
possible editions are remasters), `preferred_countries` (ordered; the first earns as many points
as the list is long; XW counts as the first; the folder's territory words are used when no
edition is matched), `preferred_sources` (ordered the same way), `preferred_depth` (16, 24 or
any: ±2), `avoid_flags` and `prefer_flags` (±3 per marker, matched against the folder's flags and
territories and the editions' names and formats).

The **recommendation card** appears once the search, matching and log fetches have settled.
Eligible folders are those at the best tier present; ties break on verification (disc ID or a
verified log, then catalog number or barcode), the preference score, completeness and sidecars,
the estimated time to obtain the folder, then size. The estimate is queue wait plus size over
upload speed, using the response's speed and queue length or the speed measured on a download
from that user in the last day; an unknown speed counts as slow (300 kB/s), a free slot as no
queue wait, and a queued file as 40 MB. The card names the best pick with its facts, offers
**Download as <preferred edition>**, lists a runner-up, and **Why not the others?** gives one
reason per folder. The Availability column shows the same estimate.

## 10. Confirming and downloading

**Download** on a row opens a confirm form listing the album's editions, the folder's possible
ones first and ticked, with the preferred one preselected (the pinned target wins). A selected
remaster shows a warning. Submitting records the chosen edition on the download and starts it.

Downloading uses slskd's batch endpoint, one batch per disc folder (batch files land flat), under
`picker/<job>`; `lidarr/` destinations are refused so Lidarr's slskd plugin never claims the
picker's downloads. Per-file states: pending, enqueued, done, limited (refused by the uploader's
queue limit: "Too many megabytes" or "Too many files"; requested again as room frees),
retry_wait (failed after slskd's own in-place retries; the picker re-requests it after
`retry_delay_seconds` × the try number, up to `download_retries` times), failed. Big folders are
requested in chunks of `chunk_files` / `chunk_mb`. A **queue timeout** (`queue_timeout_hours`, 6)
covers waiting in an uploader's queue, with the live position shown; a **stall timeout**
(`stall_timeout_minutes`, 30) counts only once bytes have flowed and stopped. A download that
ends with some files failed is **partial**: the Downloads page offers retrying the failed files
or **Try next candidate**. **Give up and try next candidate** stops a running download and
starts the next-best folder that could be the same edition.

## 11. The import pipeline

A finished download starts an import. Each step is recorded so a restart resumes where it
stopped.

1. **verify.** File count equals the request; `flac -t` and a mutagen read on every file. A
   file that fails is downloaded again automatically once (Soulseek has no hashes; a damaged
   transfer can finish as "Completed, Succeeded"). Spectrograms (sox) of the longest and a
   middle track. **Spectrum check:** every track decoded in three 10-second windows with ffmpeg;
   a cliff of at least `cutoff_db` (30 dB) below the top of the band means a lossy source
   (reported with the bitrate it is consistent with), a 96 kHz file with nothing above about
   22 kHz means upsampling; drops within 1 kHz of Nyquist on CD audio count as full-range. The
   album verdict is the worst track's. **Rip verification** (section 12).
2. **tags.** The uploader's tags against the chosen edition (section 13).
3. **review.** With `auto_import` on (default), the import continues by itself when every check
   passed: integrity, a spectrum check that ran and found nothing, a chosen edition, a tag check
   that found no different or mixed edition, and no track that differs from the verified rip.
   Anything else waits, and the event log says why. With `auto_import` off, everything waits.
   The Review page offers Import, Change edition (which reruns the checks against the new
   edition), Reject (deletes the download only), Re-download damaged or differing tracks, Force
   apply after a beets skip, Retry after a block.
4. **artist.** Lidarr must have the artist; if not, it is added with the configured root folder,
   quality and metadata profiles, `lidarr_monitor` (none) and `lidarr_monitor_new_items` (new),
   never searching the back catalogue, and the picker waits for the refresh. The artist's folder
   must lie inside the music directory. Without Lidarr, the artist folder is the cleaned artist
   name under the music directory.
5. **place.** The download is moved (rename, or copy across filesystems) to
   `<artist folder>/<album folder>`, the album folder named by `album_folder_template` the way
   Lidarr's FileNameBuilder would (`{Album Title} ({Release Year})` by default, with Lidarr's
   title and year when it has the album, and the picker's extra tokens for catalog number,
   label, country, media, edition date). An existing target blocks.
6. **beets.** `beet import -q --search-id <release>` on the folder in place, with the base config
   in `/config/beets/config.yaml` (generated once; `plugins: musicbrainz fetchart`, copy and move
   off, write on) and the `pinned.yaml` overlay, which gives zero weight to album-level tags the
   picker is about to overwrite (release and recording IDs, country, label, catalog number,
   disambiguation, year, media, disc count) so another pressing's tags don't block the match.
   Success is judged by asking the beets library whether the release is now at that folder. A
   skip stores beets' own distance breakdown (`picker/beets_explain.py`: rating against the
   threshold, album-level penalties, and per file the paired track and what differed) for the
   Review page; **Force apply** reruns with `force.yaml` (accept any distance, lift the
   missing/unmatched caps). The tags written are snapshotted for History.
7. **lidarr.** The chosen release is set as the album's single monitored release (Lidarr
   misbehaves with two), the artist folder is rescanned, and the track-file count is checked.
8. **plex.** autoscan's manual trigger for the exact folder (a single rename can slip past
   inotify) and/or a Plex partial scan. Neither failing undoes the import.

## 12. Rip verification

For folders of 16-bit 44.1 kHz stereo audio, the picker checks the decoded tracks against the
two databases of real CD rips. The disc's table of contents comes from the EAC/XLD log in the
folder when its track lengths fit the files, otherwise from the track lengths rounded up to whole
sectors as CUETools does (hidden tracks and dropped gaps defeat this and read as "not in
database"). Tracks are decoded one at a time with ffmpeg; lookups are cached for a day.

- **CUETools DB:** one lookup per disc by TOC (`fuzzy=1`); the CRC32 of the disc's audio minus
  5880 samples at the start and 5880 + 588 × (sectors mod 10) at the end, plus one CRC32 per
  track, compared with the entries for this pressing. Verified (confidence N) when the disc CRC
  matches at some offset.
- **AccurateRip:** the v1 and v2 track checksums (track 1 skips its first 2939 samples, the last
  track its last 2940; v2 adds the high words of the 64-bit products), compared per track with
  the `dBAR` record for the disc IDs; the frame-450 sums over all offsets in ±2939 find a
  pressing offset, and the checksums are recomputed there. `accuraterip` turns this database
  off, since its owners restrict third-party access; CUETools DB stays on.

Verdicts: verified, not in database, no match, or **mismatch** (tracks that differ while their
siblings verify). Only a mismatch holds the import; the Review page then offers re-downloading
just those tracks. The formulas follow whipper and CUETools, which agree; the IDs reproduce
whipper's test vector; live responses parse.

## 13. The tag check

Before anything is placed, the files' tags (the most common value per tag across the tracks) are
compared with the chosen edition: **same** (the files carry its MusicBrainz ID), **sibling**
(another release of the album with the same year and kind of edition: a note), **different**
(another edition: remaster, reissue, another year, different edition words, or a release outside
the album; or, without an ID, a remaster word when the chosen edition isn't one, or a date tag
from another year), **mixed** (more than one release ID between the files), **agree**,
**untagged**. Different and mixed hold the import; the Review page shows what the tags say and
offers **Use that edition instead** when the tagged release is an edition of this album.

## 14. Lidarr's own grabs

Lidarr's Webhook connection posts to `/hooks/lidarr` (HTTP Basic auth, constant-time compare)
on import and upgrade. The payload carries the album's Lidarr ID and the final track-file paths
after renaming; the release is the album's single monitored release. A retag job then runs beets
on that folder in place, pinned to it, debounced per album for 60 seconds. These imports appear
in History with their path unchanged. Lidarr must have `writeAudioTags` off; the picker's slskd
destinations never start with `lidarr/`, which is all Lidarr's slskd plugin claims.

## 15. Pages

| Page | Purpose |
| --- | --- |
| `/` | Find an album; Stuck in Lidarr; active jobs; last imports; count of imports waiting. |
| `/album/<rg>` | Already-have banner, Editions panel, recommendation card, candidates table, event log. The table is kept live by polling a row-version state endpoint and replacing only rows that changed, leaving expanded rows alone. |
| `/downloads` | Every download with progress, chunking, queue position, retries, and the import's state. |
| `/review` | Every import waiting on the user, with the checks as badges and the buttons inline. |
| `/imports/<id>` | The Review page for one import: edition, holds, tag check, rip verification, spectrum check, spectrograms, integrity, beets breakdown and output, tags written. |
| `/history` | Every import, with badges and the tags written. |
| `/library` | The library index: counts, the last pass, the Lidarr comparison (files Lidarr does not list, files Lidarr lists that are not on disk), folders without MusicBrainz IDs, unreadable files; **Index now**. |
| `/settings` | Settings by section with per-service Test buttons, the folder-name dry run, and the probe. |

Below 720 px the candidates table becomes cards with a Sort & filter sheet, the other tables
stack to labelled rows, buttons are at least 44 px tall, and nothing scrolls sideways.

## 16. Operations

- **Probe** (`python -m picker probe` or the Settings button) calls the real services read-only,
  saves scrubbed raw replies under `/config/probe/`, and compares them with what the code reads,
  including Lidarr's naming formats against the folder template.
- **Backups:** every start copies the database to `/config/backups/` with SQLite's online
  backup, keeping the newest 10.
- **Health:** path warnings at start and on every page (download and music directories, slskd
  directory mapping); beets config warnings (musicbrainz plugin missing, move or copy on).

## 17. Not built

From the v2 roadmap: Test-before-Save on Settings, the album queue with automatic searching,
the discography view, and upgrades (replacing an album already in the library). Declined by
the owner: catalog-number search, filling a partial download from another uploader,
notifications, remembering uploaders, browsing sibling folders.
