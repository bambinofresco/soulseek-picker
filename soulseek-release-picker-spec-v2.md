# Soulseek release picker: spec v2

Oct 22, 2026 · follows [soulseek-release-picker-spec.md](soulseek-release-picker-spec.md), which is built and running on Unraid.

## Overview

v1 picks an edition, downloads it, checks it and files it. v2 makes three kinds of improvement, built in this order:

1. **What ends up in the library.** See each folder's real contents before judging it, detect lossy transcodes after download, verify rips against the CUETools and AccurateRip databases, and search by catalog number.
2. **Design.** A recommendation card that weighs match quality, verification and how fast the folder will arrive; edition preferences (country, format, no remasters); a phone layout; Test-before-Save on Settings.
3. **Workflow.** A queue of albums to work through, a discography view per artist, and upgrades of albums already in the library.

**Unchanged rules**
- No download without a click. The queue searches on its own; downloading stays manual.
- beets is the only tag writer.
- Nothing under `/mediadata/media/music` is deleted. An upgrade moves the old folder out to a holding area, never deletes it.
- Every external call has a timeout and a clear error on the page that triggered it.

**Facts to verify before building** (each phase starts by reading the source or probing the real service; the same approach as v1's probe)
- slskd 0.26.1 `POST /api/v0/users/{username}/directory`: request body, response shape, how it reports an offline user, and whether it returns subdirectories.
- Which Soulseek clients return only files whose path matches the search words (Nicotine+, SoulseekQt, slskd), so the sidecar gap is understood rather than assumed.
- CUETools DB: the `lookup2.php` API, its XML, and the exact CRC the database stores (CUETools source, `CUETools.CTDB` and `CUETools.CDRepair`). AccurateRip: the v1/v2 track CRCs and the `dBAR` URL layout (whipper's `accurip.py` is a Python reference).
- `sox stat -freq` output, or decode with ffmpeg and FFT in numpy.
- MusicBrainz `release-group` browse by artist: paging and the `primary-type`/`secondary-types` fields.

## Part 1: what ends up in the library

### 1.1 Complete folder listings

**Problem.** A Soulseek search response is not the folder. Peers return only files whose path matches the query words, and slskd caps files per response. A folder can show 10 of 12 tracks and no `.log`, so track counts, durations, sidecar icons and the log prefetch all work from an incomplete list. In v1's first real runs, logs were only found when the folder *name* happened to match the query.

**Fix.** After grouping, fetch the real listing of each promising folder from the uploader and rebuild the candidate from it.

- **Which folders:** every candidate that passes the default filter, best-ranked first, up to `listing_limit` (default 40) per search; at most 5 users at a time; 20 s timeout per user. Others get a **Browse folder** button (the v1 action that was never built).
- **Call:** `POST /api/v0/users/{username}/directory` `{directory}` for the candidate folder and, for multi-disc candidates, each disc subfolder. Cache by `(username, folder)` for the session; a repeat search within a day reuses it.
- **Merge:** the listing replaces the response's file list for that folder (files, sizes, per-file attributes where the listing has them). Files the search returned but the listing lacks are kept and flagged, since a listing can also be partial.
- **Then:** re-run parsing, matching and log-prefetch eligibility for that candidate; the row updates in place (the v1 poller already handles per-row updates). The row shows a **listed** mark; until then, counts are shown as "10+ (search result)".
- **Browse folder** also opens a panel with the folder's tree and the names of its sibling folders under the same parent, so the uploader's other editions of the album are one click away ("also has: `OK Computer [Japan]`, `OK Computer (Vinyl)`"). Clicking a sibling adds it as a candidate.

**Done when** on 5 real albums, candidates with a listing show the same file count as slskd's own browse view, and log prefetch finds logs that the search response did not include.

### 1.2 Transcode detection

**Problem.** A FLAC made from an MP3 passes `flac -t`. The v1 spectrogram is there to eyeball.

**Fix.** Measure where the audio's spectrum ends and say what it looks like.

- **Analysis:** decode three 10-second windows (start, middle, end of the longest track and of one middle track) with ffmpeg to float PCM; FFT in numpy; average the power in bands relative to the 1–14 kHz level: 14–16, 16–18, 18–19.5, 19.5–20.5, 20.5–22 kHz, and above 22 kHz for files over 44.1 kHz.
- **Verdict per file**, then per album (worst file wins):
  - **full-range:** energy continues to within 1 kHz of Nyquist
  - **cutoff at ~N kHz:** a drop of more than `cutoff_db` (default 30 dB) between adjacent bands with nothing above; reported with the likely source: ~16 kHz → 128 kbps MP3, ~18.5–19 kHz → 192 kbps, ~19.5–20.5 kHz → 256–320 kbps or V0; approximations to tune on fixtures
  - **upsampled:** a 48/88.2/96 kHz file with nothing above 22 kHz
  - **unknown:** quiet or short material where the bands are below the noise floor
- Some masterings are genuinely band-limited (old tape, some 1980s digital, deliberate low-pass). A cutoff is therefore a **warning on the Review page**, never an automatic reject: "Looks like a transcode from ~192 kbps MP3 (cutoff 18.6 kHz on 2 of 2 tracks checked)". The spectrogram images stay beside it.
- **Settings:** `transcode_check` on/off; `cutoff_db`.
- **Tests:** synthesize FLACs with sox (white noise, full-band and low-passed at 16 / 19 / 20 kHz; an upsampled 96 kHz file) and assert the verdicts; skip when sox is missing.

**Done when** the synthetic fixtures are classified correctly and a known MP3-sourced FLAC from a real download is flagged while a known CD rip is full-range.

### 1.3 Rip database verification

**Problem.** `flac -t` proves a file decodes, not that it holds the right audio. The Rephlexions case (44 garbled bytes mid-file) happened to break decoding; a flipped sample would not.

**Fix.** Check the downloaded tracks against the databases that store checksums of real CD rips.

- **Inputs:** the TOC from the folder's rip log (v1 already computes the disc ID from it) or, without a log, a TOC built from the track lengths (rounded to frames; accepted only when every track length is known).
- **CUETools DB (primary):** look up by TOC, compute the CRC the database stores over the decoded audio, compare with the entries for this pressing. Report *verified (confidence N)*, *no match*, or *not in database*. The exact CRC and any offset handling come from CUETools' source; the spec does not restate them.
- **AccurateRip (cross-check):** compute AR v1 and v2 track CRCs (first track skips the first 5 frames, last track the last 5), fetch the `dBAR-…bin` record for the disc IDs, compare per track. Report tracks matched / confidence.
- **Where it runs:** in the import pipeline after the integrity check, before Review. Results on the Review page and in History: a per-track table (track, CTDB, AR v1/v2, confidence). A file that decodes but matches nothing while its siblings match is called out: "track 3 differs from the verified rip; re-download it?" using the v1 re-download path.
- Verification is a **signal, not a gate**: no match is common for obscure pressings and for vinyl or WEB sources, where it is skipped. `auto_import` stays off by default; when it is on, a *mismatch* (siblings verified, this track not) blocks the import for review.
- **Tests:** fixture audio with known CRCs (compute once with CUETools/whipper, store in the fixture file); mocked database replies; the AR CRC function checked against whipper's.

**Done when** a real CD-sourced download of a common album reports *verified* in both databases, and a deliberately corrupted sample in one track reports that track as mismatched.

### 1.4 Search by catalog number

- When a target edition is pinned, or when the user clicks **Search for this edition** in the editions panel, run extra Soulseek searches for the edition's catalog numbers in their common spellings (`TOCP-50201`, `TOCP50201`, `TOCP 50201`) and for its barcode. Results merge into the same candidate table (v1's grouping already merges queries); rows found this way carry a **found by catalog no.** mark.
- Also run these searches automatically when the primary search leaves the top candidates at Medium or below and some edition has a catalog number.
- Searches stay serialised (one slskd search POST at a time).

**Done when** pinning an edition whose catalog number appears in folder names on Soulseek adds those folders to the table at Exact.

## Part 2: design

### 2.1 Recommendation card

At the top of the album page, above the table, once the search and the listings have settled:

> **Best pick:** alice · `1997 - OK Computer [CDP 7243 8 55229 2 5]`
> Exact by disc ID · EAC log, AccurateRip 12/12 · complete, cover art · 16/44.1 · free slot, ~2.4 MB/s → about 3 minutes
> [Download as 1997 · US · CD · Capitol · CDP 7243…] [Why not the others?]
> Runner-up: dave (Exact by catalog number, no log, queue of 7)

- **Eligible:** candidates at the best tier present (Exact if any Exact exists). Ties broken, in order, by:
  1. verification: disc ID or verified log beats catalog/barcode beats the rest
  2. edition preference score (2.2)
  3. completeness and sidecars (all tracks, cover art, log, cue)
  4. **estimated time to obtain:** queue wait + size ÷ upload speed, using the response's `uploadSpeed` and `queueLength` and, once a download has started from that user in this session, the measured speed; an unknown speed counts as slow; a free slot counts as no queue wait
  5. smaller size at equal quality
- The card's Download opens the same confirm-edition form as the row, with the preferred edition preselected.
- **Why not the others?** expands a short list: "bob: one of 3 editions, no log; carol: MP3; erin: 13 tracks vs 12".
- The Availability column in the table shows the same estimate ("~3 min", "~40 min queue").

### 2.2 Edition preferences

A **Preferences** block on Settings, used by the recommendation card, the default row order, the "one of N" preselection and the confirm form:

| Setting | Default | Effect |
| --- | --- | --- |
| `prefer_original` | on | earliest edition first (v1) |
| `avoid_remasters` | on | editions whose title or disambiguation says remaster, and folders flagged remaster, rank below everything else; the confirm form shows a warning when a remaster is selected |
| `preferred_countries` | `GB, US, XE, JP` (ordered) | editions from earlier countries rank higher; others after; `XW` (worldwide) counts as any |
| `preferred_sources` | `CD, Vinyl, WEB` (ordered) | folder source and edition format |
| `preferred_depth` | `16` or `24` or `any` | 24-bit folders rank above or below 16-bit; `any` ignores it |
| `avoid_flags` | empty | edition markers to push down, e.g. `Deluxe, Anniversary` |
| `prefer_flags` | empty | markers to pull up, e.g. `MFSL, Japan` |

Scores are additive and tunable in one table like v1's match weights; the tiers stay the contract. A row shows a small **pref** badge with the reasons on hover ("+ GB, + CD, − remaster").

### 2.3 Phone layout

- Below 720 px the candidates table becomes a list of cards: match tier and edition, uploader, source/format, tracks, estimated time, the Download button, and a **details** toggle for the rest. Filters and sort move into a single **Sort & filter** sheet.
- The Review page stacks its sections; spectrograms and the tag table scroll horizontally inside their boxes.
- Downloads, History and Settings tables collapse to two-column rows (label, value).
- Buttons are at least 44 px tall; the sticky Actions column is not used on phones (each card has its button).
- Checked in Chrome's device mode at 390 × 844 and on a real phone; no horizontal page scroll anywhere.

### 2.4 Settings: Test before Save

**Test** sends the card's current field values with the request, so a connection can be tested before saving. Secrets typed but not saved are used for that test only and never echoed back. The small print "save first" goes away.

## Part 3: workflow

### 3.1 Queue

A list of albums to work through, on its own page and summarised on Home.

- **Add from:** search results (Add to queue next to Pick), the discography view, the Stuck-in-Lidarr list (**Add all**), pasted MusicBrainz URLs (one per line).
- **Automatic steps:** searching, listings, log prefetch and matching run for queued albums one after another (slskd allows one search at a time), so by the time the user opens an album, its table and recommendation card are ready. Downloads are never started automatically.
- **Per-album status:** queued → searched (best tier, recommended folder) → downloading → review → imported / skipped, with the recommendation card's one-line summary in the list.
- **Review list:** `/review` shows every import waiting on the user, with the integrity, transcode and verification results and the Import / Reject buttons inline, so a batch can be cleared without opening each page.
- **Skip** removes an album from the queue without searching; **Snooze** hides it for N days.

### 3.2 Discography view

`/artist/<mbid>`: every release group for the artist from MusicBrainz, filterable by type (album, EP, single, live, compilation), with for each:

- **In library:** yes/no, from beets (`mb_releasegroupid`) and Lidarr's track-file count; the edition held (label, catalog number, date) and its bit depth
- **Lidarr:** monitored, wanted, or not in Lidarr
- **Actions:** Pick, Add to queue, Upgrade (3.3) when something is already held

Reached from search results (artist name link), the album page header, and a search box that accepts an artist name or MusicBrainz artist URL. Cached like editions (7 days).

### 3.3 Upgrades

Replace an album already in the library with a better rip or a different edition, without ever deleting from `/mediadata/media/music`.

- **Entry:** the album page shows **You have:** the held edition, bit depth, import date, source (picker, Lidarr or unknown) and its verification results if the picker imported it. The recommendation card then says whether the best pick is an improvement ("24/96 vs your 16/44.1", "verified rip vs unverified", "original 1997 edition vs your 2009 remaster") and offers **Download as upgrade**.
- **Import with replacement:** the pipeline runs as usual up to Place. Place then:
  1. moves the new folder to `<album folder>.picker-new` next to the old one
  2. runs beets on it, pinned to the chosen release
  3. moves the old folder to `/mediadata/soulseek/complete/picker/replaced/<artist>/<album> (<date>)/` (the holding area; same filesystem, so a rename)
  4. renames `.picker-new` to the final name
  5. updates beets (remove the old album entry), Lidarr (select the edition, rescan) and autoscan/Plex as in v1
- **Holding area:** listed on the History page with a **Restore** button (reverses the swap) and purged after `replaced_keep_days` (default 30) by the picker, with a note in History. Nothing is purged while a restore is possible within that window.
- A Lidarr-managed album that Lidarr later "upgrades" on its own goes through the v1 webhook retag as before; the holding area is not involved.

**Done when** a 16/44.1 album in the library is replaced by a 24/96 download, Plex shows the new files, the old folder is in the holding area with Restore working, and Lidarr shows the album present with the new edition.

## Build phases

| Phase | Delivers | Done when |
| --- | --- | --- |
| 1. Listings | Folder listings for ranked candidates, Browse folder, sibling folders | 1.1 |
| 2. Transcode detection | Band analysis, verdicts, Review warning, fixtures | 1.2 |
| 3. Rip verification | CTDB and AccurateRip checks in the pipeline, per-track results | 1.3 |
| 4. Catalog search | Extra searches on pin or weak match, found-by marks | 1.4 |
| 5. Recommendation and preferences | Card, time estimate, preference settings and badges | A pinned preference changes the recommended folder as expected on 5 real albums |
| 6. Phone layout and Test-before-Save | 2.3, 2.4 | No horizontal scroll at 390 px; Test works unsaved |
| 7. Queue and review list | 3.1 | Ten queued albums are searched and matched unattended; the review list clears them |
| 8. Discography | 3.2 | An artist page shows held editions and Lidarr state correctly for a known artist |
| 9. Upgrades | 3.3 | The 3.3 acceptance test |

Each phase ships with tests (unit for the pure parts, fixture-driven for analysis and verification, mocked services for the rest) and a README section. The probe gains checks for any new external call.
