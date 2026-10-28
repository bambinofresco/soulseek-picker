"""The M3U8 file and the Plex playlist (picker/plexsync.py), against an in-memory Plex."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from picker import library, plexsync
from picker.db import session
from picker.models import Import, Job, Playlist, PlaylistTrack, QueueItem
from tests.test_library import make_flac

REC_A, REC_B, REC_C = ("99999999-0000-0000-0000-00000000000" + d for d in "abc")


class FakePlex:
    """Tracks by file path; playlists with ordered items. Mirrors the endpoints the client wraps."""

    def __init__(self, files: dict[str, str]):
        self.files = files  # plex path -> rating key
        self.titles = {}  # rating key -> title
        self.lists: dict[str, dict] = {}
        self.next_item = 100
        self.created = 0
        self.calls = []

    def _track(self, key):
        path = next(p for p, k in self.files.items() if k == key)
        return {"ratingKey": key, "title": self.titles.get(key, Path(path).stem), "Media": [{"Part": [{"file": path}]}]}

    async def tracks_titled(self, title):
        self.calls.append(("tracks_titled", title))
        return [self._track(k) for k in self.files.values() if title.lower() in self.titles.get(k, "").lower()]

    async def albums_titled(self, title):
        self.calls.append(("albums_titled", title))
        return [{"ratingKey": "album-1", "title": title}]

    async def children(self, key):
        return [self._track(k) for k in self.files.values()]

    async def playlists(self):
        return [{"ratingKey": k, "title": p["title"]} for k, p in self.lists.items()]

    async def create_playlist(self, title, keys):
        self.created += 1
        key = f"pl-{self.created}"
        self.lists[key] = {"title": title, "items": []}
        await self.add_items(key, keys)
        return {"ratingKey": key, "title": title}

    async def playlist_items(self, key):
        return [{"ratingKey": k, "playlistItemID": i, **{"Media": self._track(k)["Media"]}} for i, k in self.lists[key]["items"]]

    async def add_items(self, key, keys):
        for k in keys:
            self.next_item += 1
            self.lists[key]["items"].append((str(self.next_item), k))
        self.calls.append(("add", list(keys)))

    async def remove_item(self, key, item_id):
        self.lists[key]["items"] = [(i, k) for i, k in self.lists[key]["items"] if i != item_id]
        self.calls.append(("remove", item_id))

    async def move_item(self, key, item_id, after):
        items = self.lists[key]["items"]
        moving = next(x for x in items if x[0] == item_id)
        items.remove(moving)
        if after is None:
            items.insert(0, moving)
        else:
            idx = next(i for i, x in enumerate(items) if x[0] == after)
            items.insert(idx + 1, moving)
        self.calls.append(("move", item_id, after))

    def order(self, key):
        return [k for _, k in self.lists[key]["items"]]


@pytest.fixture
def plex(settings, monkeypatch):
    settings.plex_url, settings.plex_token, settings.plex_section_id = "http://plex", "t", "3"
    settings.plex_music_path = "/plex/music"
    fake = FakePlex({})

    class Services:
        pass

    Services.plex = fake
    monkeypatch.setattr(plexsync, "services", lambda: Services())
    return fake


def make_playlist(settings, rows):
    with session() as s:
        playlist = Playlist(name="Road: Trip?", state="running")
        s.add(playlist)
        s.commit()
        s.refresh(playlist)
        for n, (title, rec) in enumerate(rows, 1):
            s.add(PlaylistTrack(playlist_id=playlist.id, position=n, title=title, artist="Nirvana", album="Nevermind",
                                duration_ms=200_000, recording_mbid=rec, resolution="isrc"))
        s.commit()
    return playlist


def add_file(settings, plex, name, rec, title=None):
    path = settings.music_dir / "Nirvana" / "Nevermind (1991)" / name
    make_flac(path, 1, seconds=200, title=title or Path(name).stem[3:], artist="Nirvana", musicbrainz_trackid=rec)
    key = f"t{len(plex.files) + 1}"
    plex.files[plexsync.plex_file_path(str(path), settings)] = key
    plex.titles[key] = title or Path(name).stem[3:]
    return path, key


def test_paths_and_names(settings):
    settings.plex_music_path = "/plex/music"
    assert plexsync.plex_file_path(str(settings.music_dir / "A" / "B (1)" / "01.flac"), settings) == "/plex/music/A/B (1)/01.flac"
    assert plexsync.plex_file_path("/elsewhere/x.flac", settings) == "/elsewhere/x.flac"
    assert plexsync.safe_name('Road: Trip? <2026>') == "Road Trip 2026"
    assert plexsync.safe_name("...") == "Playlist"


async def test_m3u_only_when_plex_is_off(settings):
    settings.playlist_dir = settings.config_dir / "playlists"
    playlist = make_playlist(settings, [("Smells Like Teen Spirit", REC_A), ("Breed", REC_B)])
    path, _ = add_file(settings, FakePlex({}), "01 Smells Like Teen Spirit.flac", REC_A)
    library.scan(settings.music_dir, settings)
    result = await plexsync.sync(playlist.id, settings)
    assert result.have == 1 and result.tracks == 2 and result.plex.state == "off"
    m3u = Path(result.m3u)
    assert m3u.name == "Road Trip.m3u8" and m3u.parent == settings.playlist_dir
    text = m3u.read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U\n#PLAYLIST:Road: Trip?\n")
    assert "#EXTINF:200,Nirvana - Smells Like Teen Spirit\n../../music/Nirvana/Nevermind (1991)/01 Smells Like Teen Spirit.flac\n" in text
    assert "# 2. Nirvana - Breed (not in the library yet)" in text
    with session() as s:
        row = s.get(Playlist, playlist.id)
    assert row.m3u_path == str(m3u) and row.last_sync and "Plex not configured" in row.sync_note


async def test_plex_create_add_remove_and_reorder(settings, plex):
    settings.playlist_dir = settings.config_dir / "playlists"
    playlist = make_playlist(settings, [("Smells Like Teen Spirit", REC_A), ("Breed", REC_B), ("Lithium", REC_C)])
    add_file(settings, plex, "01 Smells Like Teen Spirit.flac", REC_A)
    _, key_c = add_file(settings, plex, "05 Lithium.flac", REC_C)
    library.scan(settings.music_dir, settings)

    result = await plexsync.sync(playlist.id, settings)
    assert result.plex.state == "ok" and result.plex.created and result.plex.added == 2 and result.plex.missing == []
    assert plex.order("pl-1") == ["t1", key_c]
    with session() as s:
        row = s.get(Playlist, playlist.id)
        tracks = s.exec(select(PlaylistTrack).order_by(PlaylistTrack.position)).all()
    assert row.plex_rating_key == "pl-1" and "created" in row.sync_note
    assert [t.plex_item_id for t in tracks] == ["101", None, "102"]

    # Breed lands; it must slot in between.
    _, key_b = add_file(settings, plex, "02 Breed.flac", REC_B)
    library.scan(settings.music_dir, settings)
    result = await plexsync.sync(playlist.id, settings)
    assert result.plex.added == 1 and result.plex.moved == 1 and not result.plex.created
    assert plex.order("pl-1") == ["t1", key_b, key_c]

    # Nothing changed: a sync is idempotent.
    plex.calls.clear()
    result = await plexsync.sync(playlist.id, settings)
    assert result.plex.added == result.plex.removed == result.plex.moved == 0
    assert not [c for c in plex.calls if c[0] in ("add", "remove", "move")]

    # A row dropped from the playlist leaves Plex; the same track twice stays twice.
    with session() as s:
        tracks = s.exec(select(PlaylistTrack).order_by(PlaylistTrack.position)).all()
        tracks[0].dropped = True
        s.add(tracks[0])
        s.add(PlaylistTrack(playlist_id=playlist.id, position=4, title="Lithium", artist="Nirvana", album="Nevermind",
                            recording_mbid=REC_C, resolution="isrc"))
        s.commit()
    result = await plexsync.sync(playlist.id, settings)
    assert result.plex.removed == 1 and result.plex.added == 1
    assert plex.order("pl-1") == [key_b, key_c, key_c]

    # The user deleted the Plex playlist: a new one is made.
    plex.lists.clear()
    result = await plexsync.sync(playlist.id, settings)
    assert result.plex.created and plex.order("pl-2") == [key_b, key_c, key_c]


async def test_track_not_yet_in_plex_is_partial_and_found_through_its_album(settings, plex):
    settings.playlist_dir = settings.config_dir / "playlists"
    playlist = make_playlist(settings, [("Smells Like Teen Spirit", REC_A), ("Breed", REC_B)])
    path_a, _ = add_file(settings, plex, "01 Smells Like Teen Spirit.flac", REC_A)
    path_b = settings.music_dir / "Nirvana" / "Nevermind (1991)" / "02 Breed.flac"
    make_flac(path_b, 2, title="Breed", artist="Nirvana", musicbrainz_trackid=REC_B)  # on disk, not in Plex yet
    library.scan(settings.music_dir, settings)
    plex.titles["t1"] = "Something Else"  # the title filter misses it; the album lookup finds it
    result = await plexsync.sync(playlist.id, settings)
    assert result.plex.state == "partial" and result.plex.missing == ["Nirvana - Breed"]
    assert ("albums_titled", "Nevermind") in plex.calls and plex.order("pl-1") == ["t1"]


async def test_job_retries_while_plex_catches_up(settings, plex):
    settings.playlist_dir = settings.config_dir / "playlists"
    playlist = make_playlist(settings, [("Breed", REC_B)])
    path_b = settings.music_dir / "Nirvana" / "Nevermind (1991)" / "02 Breed.flac"
    make_flac(path_b, 2, title="Breed", artist="Nirvana", musicbrainz_trackid=REC_B)
    library.scan(settings.music_dir, settings)
    job = plexsync.queue_sync(playlist.id, timedelta(0))
    assert plexsync.queue_sync(playlist.id, timedelta(seconds=30)) is None  # merged into the queued one
    with session() as s:
        queued = s.exec(select(Job).where(Job.kind == "plexsync")).all()
    assert len(queued) == 1 and queued[0].run_after is not None
    await plexsync.run_sync(job)
    with session() as s:
        jobs = s.exec(select(Job).where(Job.kind == "plexsync").order_by(Job.id)).all()
    assert len(jobs) == 1 and jobs[0].payload["retries"] == 1  # merged into the still-queued job, with the retry counted
    # Plex scanned: the retry finds it and stops asking for itself.
    plex.files[plexsync.plex_file_path(str(path_b), settings)] = "t9"
    plex.titles["t9"] = "Breed"
    with session() as s:
        jobs[0].status = "running"
        s.add(jobs[0])
        s.commit()
    await plexsync.run_sync(jobs[0])
    with session() as s:
        assert len(s.exec(select(Job).where(Job.kind == "plexsync", Job.status == "queued")).all()) == 0
    assert plex.order("pl-1") == ["t9"]


async def test_done_items_folder_is_indexed_before_the_sync(settings, plex):
    settings.playlist_dir = settings.config_dir / "playlists"
    playlist = make_playlist(settings, [("Breed", REC_B)])
    folder = settings.music_dir / "Nirvana" / "Nevermind (1991)"
    make_flac(folder / "02 Breed.flac", 2, title="Breed", artist="Nirvana", musicbrainz_trackid=REC_B)
    plex.files[plexsync.plex_file_path(str(folder / "02 Breed.flac"), settings)] = "t1"
    plex.titles["t1"] = "Breed"
    with session() as s:
        imp = Import(rg_mbid="rg", state="imported", step="done", album_dir=str(folder))
        s.add(imp)
        s.commit()
        s.refresh(imp)
        item = QueueItem(rg_mbid="rg", state="done", import_id=imp.id, playlist_id=playlist.id)
        s.add(item)
        s.commit()
        s.refresh(item)
        track = s.exec(select(PlaylistTrack)).first()
        track.queue_item_id = item.id
        s.add(track)
        s.commit()
    assert library.album_files("rg") == []  # the index has not seen the folder
    result = await plexsync.sync(playlist.id, settings)
    assert result.have == 1 and plex.order("pl-1") == ["t1"]
