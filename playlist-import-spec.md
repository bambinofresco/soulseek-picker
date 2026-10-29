# Playlist import and upgrades: draft specification (v3)

Status: **built, 28 October 2026**, in seven commits (library index, playlists, queue, playlist
sync, upgrades, tracks-only, Update). SPEC.md sections 17 to 23 now describe the code and win
where this document differs. Known differences: split MusicBrainz release groups are shown as
separate rows rather than merged (section 3.3 here); albums Lidarr's profile will not list
still get their artist added (the album is simply not listed, and the import notes it); the
Exact-beats-tags rule applies to every import, not only runs. This document stays as the
record of the design and the decisions behind it.

## 0. Summary

The user exports a Spotify playlist to CSV (Exportify or similar; the picker builds no exporter
and never talks to Spotify) and uploads it. The picker resolves every row to a MusicBrainz
recording and release group, shows the playlist grouped by album, lets the user choose per album
between the whole album, only the playlist's tracks, an upgrade of a copy already owned, or skip,
then works the chosen albums through a queue: search, match, pick the recommended folder when the
match is good enough, download, import. As albums land, the picker builds the playlist in Plex
(and as an M3U8 file) in the playlist's order, from the tracks already in the library and the
ones it obtains.

The feature stands on three things that already exist: edition matching and tiers (SPEC §7), the
edition preferences and recommendation card (§9), and the import pipeline (§11). It also builds
two things v2 planned and never got (§17): the album queue, and upgrades of albums already held.

## 1. Guarantees, amended

- **Nothing under the music library is deleted or overwritten.** Amended for upgrades: the
  replaced copy is *moved out* of the library to `<replaced_dir>/<artist>/<album> (<date>)`,
  default `/mediadata/media/replaced`, beside the music folder rather than under the soulseek
  folder, so neither Plex nor slskd's shares can see it. It is never deleted by the picker. The
  Settings page shows how much space the replaced folder holds and offers a one-click **Delete
  replaced copies older than N days**. That click is the only deletion the picker ever performs.
- **Downloads are never started without a click.** Amended: *one click authorizes one run.*
  Pressing **Start run** on the review page authorizes every download in that run. Within a
  run the picker downloads only folders the recommendation card would have named, and only when
  the best tier reaches `auto_pick_min_tier` (default High). Anything weaker waits for the user.
- **beets is the only program that writes tags.** Unchanged.
- **Plex playlists the picker did not create are never touched.** New. The picker records the
  Plex playlist it made and edits only that one.

## 2. Input: the CSV

Any CSV with a header row. Columns are recognised by name, case-insensitively, with the Exportify
names first: `Track URI`, `Track Name`, `Album Name`, `Artist Name(s)`, `Album Artist Name(s)`,
`Release Date`, `Duration (ms)`, `ISRC`, `Disc Number`, `Track Number`, `Added At`. Required:
a title, an artist and an album. Strongly wanted: `ISRC` and `Duration (ms)`; without them
resolution falls back to text matching (section 3) and the review page says so per row.

The playlist's name is taken from the file name, editable on the review page. Uploading a CSV
with the same name as an existing playlist offers **Update** (re-resolve, keep the existing
choices for albums already decided, show new rows) or **New playlist**.

## 3. Resolving rows to MusicBrainz

A `resolve` job runs after upload, with progress on the review page (rows done / total).

1. **By ISRC.** MusicBrainz recording search `isrc:(A OR B OR ...)`, 25 ISRCs per query, so a
   1,000-track playlist resolves in under a minute instead of 17 at one request per second.
   Each hit gives a recording ID and the release groups it appears on. The recording is always
   the one the ISRC names: a live or edited version on the playlist stays that version, and lands
   on whatever album holds it.
2. **By text.** Rows without an ISRC hit: recording search by artist, title and duration (±3 s),
   then release-group search by the CSV's album name as a tie-breaker. The result carries a
   confidence (`isrc`, `text-strong`, `text-weak`, `none`); weak and none are listed under
   **Needs attention** on the review page with artist and album fields to resolve by hand, the
   same fields as the home page.
3. **Choosing the album.** A recording usually appears on several release groups: the studio
   album, singles, compilations, reissue groups. The CSV says which album Spotify showed, which
   is often a compilation. Rule, in order, never changing the recording:
   - the release group matching the CSV's album name, if its type is Album or EP (singles are
     handled by rule 4);
   - otherwise, with `prefer_studio_album` on (default), the earliest release group of primary
     type Album and no secondary type (not Compilation, Live, Soundtrack, Remix) that holds the
     recording;
   - otherwise the CSV's album.
   MusicBrainz keeps one recording per remaster, and the recording carrying the CSV's ISRC is
   sometimes listed only on compilations (Herbie Hancock's "Maiden Voyage - Remastered 1999"
   resolved to a 43-track Various Artists coffee-shop compilation). So when the CSV names an
   album that none of the recording's release groups is, and none of them is a studio album or
   EP by the track's artist, one release-group search by artist and album name (edition
   suffixes stripped) adds the named studio album or EP as an option, and the rule above picks
   it. The compilation stays in the row's From list.
   The review page shows the choice and offers the alternatives per album ("From: Greatest
   Hits (1998) → The Album (1973)"). A recording that appears only on singles and compilations
   (MusicBrainz often keeps a single edit or radio version as its own recording) is badged
   **only on singles and compilations**, so the user can switch the row to the album version
   by hand instead of downloading a compilation for one edit.
   Release groups that MusicBrainz splits (a deluxe edition in its own group) are merged into one
   row when they share the artist and the title after edition words are stripped; the picker's
   edition preferences then choose between them as between any two editions.
   The CSV's **Album Artist** drives the album and the Soulseek search; the track's artist list
   (Exportify's comma-separated "Artist Name(s)") is used only to resolve the recording, since
   searching Soulseek for "Artist A, Artist B" finds nothing.
4. **Singles.** When a recording was also released as a single (a Single or EP release group
   holds it), the row offers **Get: ○ the single (1977, 3 tracks) ● the album**. The default
   follows the page's top toggle: with **Full albums** on, the album, with the row's box ticked
   as usual; with it off, the single, in full (A-side and B-sides), since "the single" is the
   smallest whole release that has the song.
5. **Already owned.** Section 4 decides, per album and per track.

Every lookup is cached in `cacheentry` like the rest of MusicBrainz.

## 4. What the library already holds

Today the owned check asks three sources (picker history, Lidarr, beets), all per album. The
playlist needs per-track answers and must not trust any one source, so a fourth is added and
the check is widened.

- **Library index.** A `libraryfile` table: every audio file under the music directory with
  its path, size, modification time, duration, bit depth and rate, and the tags that identify it
  (artist, album, title, track number, MusicBrainz release, release-group and recording IDs when
  present). Built by walking the directory and reading tags with mutagen (read-only), refreshed
  by an `index` job after every import and retag (that folder only), nightly, and on demand.
  Not hourly: a walk wakes every drive in an Unraid array. A 20,000-file library indexes in a
  few minutes the first time and in seconds after, since unchanged folders are skipped by mtime.
- **Loose verification against Lidarr.** The index is compared with Lidarr's track files for
  each artist: files Lidarr doesn't know, and files Lidarr lists that aren't on disk, are shown
  on a **Library** page and counted in the health line. Nothing is changed; Lidarr's rescan
  button is one click away.
- **Per-track owned check.** A playlist track is *owned* when the index holds a file with its
  recording ID, or, failing an ID, the same artist and title (folded) with a duration within
  3 s. A name-and-duration match can pick the album version when the playlist has the single
  edit, so it is shown as **owned (by name)** on the review page and the user can untick it;
  files with MusicBrainz IDs always win over name matches. Owned tracks are listed under their
  album with a ✓ and are never downloaded; they go straight into the playlist (section 8).
- **Per-album owned check.** An album is *owned* when the picker's history, Lidarr, beets or the
  index has it. The review page shows the owned copy's edition, its preference score and whether
  it is complete, and offers **Upgrade** (section 7) when the preferences would score another
  edition higher, when the copy is partial, or when a spectrum check on the copy finds a lossy
  source. Otherwise the album is ticked off.

## 5. The review page (`/playlists/<id>`)

Albums in order of first appearance in the playlist, each a collapsed row:

```
▸ Talking Heads — Remain in Light (1980, Album)         3 of 8 tracks   owned: 1980 US LP, pref +6, complete
    ○ Full album   ○ Only these 3   ○ Upgrade   ● Skip (owned)
```

Expanding lists the playlist's tracks under it with position, title, duration, the recording's
confidence, and ✓ for tracks already owned. Controls:

- **Top toggle:** Full albums / Only playlist tracks, which sets every undecided album; per-album
  radios override it. A count line: "41 albums: 30 full, 8 tracks-only, 2 upgrades, 1 skipped,
  5 owned".
- **Prefer studio album** toggle (section 3.3). **Single or album** per row (section 3.4).
- **Needs attention** section for unresolved rows, with resolve-by-hand fields and **Drop**.
- **Start run** (section 6) and **Save for later**. Start is disabled while resolving.
- Estimated download size from the smallest edition's track count × 30 MB (FLAC CD average)
  for full albums and per track for tracks-only, so the user sees "about 14 GB" before starting.

The album row is also a link to the normal album page, where the user can search and pick by
hand; a hand-picked download for a queued album satisfies the queue item.

## 6. The queue and the run

A `queueitem` is one album the picker should obtain, with a mode (`full`, `tracks` carrying the
wanted recording IDs, or `upgrade` carrying the owned copy's path and edition), a source
(playlist ID, or the search results, the album page, the Stuck list), a state and a note. A run
is the set of queue items a playlist created; **Start run** moves them from `held` to `queued`.

States: `held` → `queued` → `searching` → `searched` → `downloading` → `importing` → `done`;
side exits `needs_pick` (match too weak), `none_found` (no FLAC folder, retried after
`queue_retry_days`, default 7, up to 3 times, using **Search more**'s longer search on the
second try), `failed` (every eligible folder failed to download), `review` (the import is
waiting on the user, as today), `skipped`.

A `queue` job runs continuously while items are queued, with these rules:

- **Searches run one at a time with a 5 s gap**, in playlist order, each followed by its log
  prefetch as today. slskd has no one-search limit, but the Soulseek server throttles or briefly
  bans clients that flood it, and a ban would also hit Lidarr's slskd plugin. A search ends
  after 10 s once 25 users have answered, which well-known albums reach, or at 45 s; up to two
  broader queries follow when few FLAC folders came back. Expect 20 to 40 s per well-known album
  and a minute or more for an obscure one, so **100 albums take roughly 30 to 60 minutes** of
  searching. Downloads run alongside, so the run's length is set by downloads, not searches.
- **Albums Lidarr won't list** (its metadata profile excludes singles, live albums, compilations
  and soundtracks by default) bypass Lidarr: the folder is placed under the artist's Lidarr
  folder when Lidarr has the artist, else under the cleaned artist name, beets tags it, Plex
  scans it, and the artist and Lidarr steps are skipped with a note. Lidarr may show the folder
  as unmatched files; the library index still counts the album as owned.
- **Various Artists releases are never auto-downloaded.** A track found only on a Various
  Artists compilation parks at `needs_pick`, badged on the review page, and the user chooses the
  compilation, another album, or **Drop**.
- **Tags versus an Exact match.** When the tag check says *different* but the folder matched
  **Exact** (a disc ID, catalog number or barcode agreed with the chosen edition), physical
  evidence wins: the import continues with a note ("tags name the 2005 remaster; disc ID says
  1980 US CD") and beets retags to the chosen edition. Any weaker match with a *different* tag
  check still holds for review, as today. This applies to every import, not only runs, since the
  reasoning is the same by hand.
- **Pick** when the search settles: the existing `recommend()` result, if its best tier is at or
  above `auto_pick_min_tier` and its edition is unambiguous enough (an ambiguity set of one, or
  all members equal under the preferences), starts the download for that edition. Otherwise the
  item parks at `needs_pick` with the recommendation card's one-line summary, and the user
  picks on the album page as today.
- **Concurrent downloads:** `queue_concurrency` (default 2). Soulseek etiquette; also keeps
  one slow uploader from blocking the run.
- **Next candidate:** a download that fails or ends partial moves to the next folder that could
  be the same edition, the way **Try next candidate** does, up to `queue_candidates` (3) folders.
- **Pause** and **Cancel run** on the run page; a cancelled run keeps finished imports and stops
  queued items; downloads already running finish (or **Give up** per download as now).
- **One import at a time.** beets is a subprocess over one SQLite library, and Lidarr queues
  its refreshes; two downloads finishing together must not run two imports at once. The verify
  step (decoding, spectrum, rip checks) may run concurrently; the artist, place, beets, Lidarr
  and Plex steps take a single lock.
- **Artists a run adds to Lidarr** are added with monitor `none` and new items `none`, whatever
  `lidarr_monitor_new_items` says. A playlist can touch dozens of artists the user wanted one
  song from; the existing default (`new`) would have Lidarr grab every future release of theirs.
  An artist added by hand from the album page keeps the existing settings.
- Restarts: queue items are rows, so a restart resumes exactly as the job runner already does.

The run page (`/playlists/<id>/run`, also the top of the review page once started) leads with
what needs the user: items waiting at review and items needing a pick, each a link to the import
or album page. Uploaders' tags often name another edition, so a run of 60 albums can leave 15
to 25 holds; the run page is where they are worked through. Below that, counts per state, the
current searches and running downloads. **Add to queue** also appears next to Pick in search results,
on the album page, and as **Add all** on the Stuck-in-Lidarr list; those items start `queued`
with a click.

## 7. Modes beyond the full album

### 7.1 Tracks-only

Downloading some of a folder is new. It touches:

- **Which files.** The chosen edition's track list (MB recording IDs per position) names the
  wanted positions. The folder's files are matched to positions by **duration first** (the
  response carries each file's length; the matcher already pairs whole folders this way), with
  the track number parsed from the file name as a tie-breaker, since many uploads have no
  numbers or disc-relative ones. If a wanted track can't be identified in the folder, the folder
  is not eligible. Logs and cues are still fetched; images too.
- **Download.** `files_to_fetch` takes the wanted subset. Everything downstream already works on
  "the request", so the file-count check holds.
- **Rip verification.** AccurateRip is per track and still works. CUETools DB's disc CRC needs
  the whole disc and is reported "not checked (partial album)".
- **Tag check, spectrum, integrity:** unchanged.
- **Placement.** The partial album goes to the normal `<artist>/<album (year)>` folder, and an
  existing folder blocks, exactly as today. Files are never added to a folder that already
  exists, so no album is ever assembled from more than one uploader's rip. When a later playlist
  wants more tracks from an album already held as a partial, the review page offers, in this
  order: **Full album** (an upgrade, 7.2, replacing the partial), **These tracks plus the ones
  you have** (also an upgrade: one uploader's folder supplying the union of wanted tracks, so the
  result is still one rip of one edition), or **Skip** (keep what you have, the playlist uses
  the owned tracks). Default: Full album.
- **beets.** `beet import` of an incomplete album pinned to the release, with a third overlay,
  `partial.yaml`, that zeroes the missing-tracks weight on top of `pinned.yaml`. Without it
  beets' default penalty on a 7-of-12 album pushes nearly every tracks-only import below the
  threshold and each would need Force apply. The unmatched-tracks cap stays, so a folder with
  files that belong to no position still skips. History records "7 of 12 tracks".
- **Lidarr.** Lidarr has two separate monitored flags. Each album has a list of releases
  (editions) of which exactly one is marked monitored; that one only tells Lidarr which
  edition's track list to match the files against. The picker already sets it to the chosen
  release for every import, so Lidarr counts "7 of 12" against the right edition instead of
  showing the files as unmatched. Separately, `album.monitored` means "search for this album and
  grab what's missing". Today the picker never changes it: for a full album nothing is missing,
  so it does no harm either way, and artists the picker adds start with no albums monitored. A
  partial album is the one case where it matters, since a monitored album with five tracks
  missing is exactly what Lidarr goes and fills from an indexer, in whatever edition it finds.
  So, new for partial albums only, the picker sets `album.monitored` **off** and notes it in the
  event log.

### 7.2 Upgrades

An upgrade replaces the library's whole copy of an album with a better one. Entry points: the
**Upgrade** radio on the playlist review page; an **Upgrade** button on the album page when the
album is owned (the already-have banner), listing the owned copy's edition, completeness, bit
depth and preference score next to the recommendation; and **Upgrade to full** on a partial
album in History. The whole album is always replaced, never only the playlist's tracks.

The queue item carries the owned copy's path, release and import ID. Search, pick and download
are the normal ones. Import runs the normal checks, then diverges:

- **Comparison.** Before placing, the Review page shows old and new side by side: edition,
  preference score, tracks, bit depth and rate, spectrum verdict, rip verification. An upgrade
  **always waits at review** unless `auto_upgrade` is on *and* the new copy passes every check
  that gates `auto_import` today *and* is strictly better: higher preference score, or complete
  where the old was partial, or clean where the old copy's spectrum check found a lossy source,
  with none of those three worse.
- **Replace.** Free space is checked first: the swap briefly holds three copies (the download,
  the old folder in the replaced folder, the new one in the library), and Unraid user shares can
  turn a rename into a copy. Then the owned folder is moved to the replaced folder (section 1);
  the new folder is placed at the template's path; beets is told to forget the old album
  (`beet remove -a` on its path, without `-d`, so nothing is deleted) and imports the new one
  pinned to its release; Lidarr rescans the artist and the monitored release is updated; Plex is
  scanned.
- **Playlists.** Any picker playlist that referenced the old files is re-synced to Plex (section
  8) once Plex has finished scanning the folder (the album's tracks are seen at the new paths),
  since Plex drops playlist items whose files have gone and gives the new files new identities.
- **Undo.** The Review page of a finished upgrade offers **Put the old copy back** while the
  replaced folder still exists: the new folder moves to the replaced folder and the old one
  returns, beets, Lidarr and Plex are updated the same way. Even undo deletes nothing.

Build order: full-album mode first, then upgrades, then tracks-only (section 12). Upgrades
reuse everything from full-album mode plus the replace step; tracks-only is the only part that
reaches into file selection.

## 8. Building the playlist

As each import reaches `imported`, and once at the start of a run for the tracks already owned,
the picker syncs the playlist:

- **M3U8 file.** `<playlist_dir>/<name>.m3u8` (default `/mediadata/media/playlists`, outside the
  music library) rewritten in full on every change, in playlist order, with `#EXTINF` lines and
  paths relative to the playlist folder. Tracks not yet obtained are omitted (a commented line
  keeps their place). Portable to any player and a durable record.
- **Plex playlist.** Plex's playlist API is undocumented but stable; it is what python-plexapi
  wraps. The picker creates a playlist named after the CSV, remembers its Plex ID, and keeps its
  items equal to the playlist's owned tracks in the CSV's order, by appending, removing and
  moving items. Plex cannot be asked "which track is this MusicBrainz recording?", so the picker
  finds each track by **file path**: it asks Plex for the album's tracks and matches each one's
  file against the path the index knows, translated with the existing `plex_music_path` mapping.

  **Why a sync step exists.** Plex learns about new files only when it scans. The picker already
  asks autoscan (and Plex directly, when configured) to scan the exact folder the moment an
  import finishes, so the scan starts at once, but it still runs in the background and Plex has
  the new tracks a few seconds later, not instantly. So after each import the picker polls Plex
  for the album, usually finding it within seconds, giving up after 10 minutes. If Plex was down,
  or the user moved files, the playlist in Plex drifts from what the picker knows. The **Sync to
  Plex** button (and an automatic nightly sync, and the one after an upgrade) is the same
  operation run over the whole playlist: look every track up again, add what's missing, remove
  what's gone, fix the order. It is idempotent and safe to press any time.

  Only the playlist the picker created (stored ID, title checked on each pass) is edited. On
  **Update** (section 2) rows removed from the CSV are removed from the Plex playlist and the
  M3U8; files are untouched.

Plex's `POST /playlists/upload` M3U import exists and would be simpler, but it creates a new
playlist on every upload and cannot update one, so it is not used.

## 9. Settings

| Setting | Default | Applies to | Meaning |
| --- | --- | --- | --- |
| `auto_pick_min_tier` | High | queue | lowest tier the queue downloads without asking |
| `queue_concurrency` | 2 | queue | downloads at once |
| `queue_candidates` | 3 | queue | folders tried per album before `failed` |
| `queue_retry_days` | 7 | queue | wait before re-searching a `none_found` album |
| `auto_upgrade` | off | upgrades | replace without review when strictly better (7.2) |
| `prefer_studio_album` | on | playlists | compilation tracks resolve to the original album |
| `playlist_dir` | /mediadata/media/playlists | playlists | where M3U8 files go |
| `replaced_dir` | /mediadata/media/replaced | upgrades | where replaced copies are moved |

None duplicate an existing setting. `download_retries` and `retry_delay_seconds` still govern
re-requests of one file within a download; `queue_candidates` is one level up, how many
folders to try. `auto_import` still decides whether a passing import places itself; the queue
adds only the decision to *start* a download. The queue settings apply to every queued album,
whatever added it, not only playlists.

## 10. Data model

| Table | What one row is |
| --- | --- |
| `playlist` | A playlist: name, CSV hash, state (resolving, review, running, paused, done), Plex playlist ID, M3U8 path, counts, last sync. |
| `playlisttrack` | One CSV row: position, Spotify URI, ISRC, title, artist, album, duration, resolved recording and release-group IDs, how it was resolved and the confidence, single-or-album choice, the queue item it belongs to, the library file once owned, the Plex item ID, state. |
| `queueitem` | One album to obtain: release group, mode (full, tracks, upgrade), wanted recordings, owned copy (path, release, import ID), source, state, note, search/download/import IDs, attempts, next try. |
| `libraryfile` | One audio file under the music directory: path, size, mtime, duration, depth and rate, identifying tags and MusicBrainz IDs, Lidarr track-file ID when Lidarr knows it, indexed at. |

## 11. Decisions taken

- No Spotify API, ever. CSV is the only input.
- The owner does not use Lidarr's Spotify import list; no racing to guard against.
- An owned album shows as owned with its preference score; **Upgrade** is offered beside it and
  built in this round, replacing the whole album.
- Singles: the single when the top toggle is off, the album when it is on, switchable per row.
- Live and edited versions: the recording the playlist names, wherever it lives.
- No setting to let Lidarr complete partial albums; partial albums stay unmonitored.
- Albums Lidarr's metadata profile won't list bypass Lidarr and live in beets and Plex.
- Various Artists releases always wait for a manual pick.
- Searches run one at a time with a 5 s gap; no concurrency setting.
- A *different* tag check does not hold an Exact match; it holds every weaker one.

## 12. Order of work

1. Library index and the Library page (section 4). Useful on its own and needed by everything
   after. **Built 28 October 2026:** `picker/library.py`, the `libraryfile` table, the `index`
   job (after imports, nightly at 03:00, Index now), the Lidarr comparison, `/library`, and the
   index as the fourth owned source. The Lidarr comparison runs on full passes only.
2. CSV upload, resolution, review page with owned marks (sections 2, 3, 5).
3. Queue, run, auto-pick, run page, **Add to queue** everywhere (section 6), full-album mode.
4. M3U8 and Plex playlist sync (section 8).
5. Upgrades (7.2).
6. Tracks-only mode (7.1).
7. Playlist Update (section 2).
