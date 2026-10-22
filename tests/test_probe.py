"""The probe against mock services shaped like slskd 0.26.1, Lidarr develop and Plex (per their source)."""

import json

import httpx
import pytest

from picker import probe

TRANSFER = {"id": "t-1", "username": "alice", "filename": r"@@music\Radiohead\OK Computer\01.flac", "size": 30_000_000,
            "state": "Queued, Remotely", "requestedAt": "2026-10-22T08:00:00.0000000Z", "bytesTransferred": 0,
            "batchId": "b-1", "placeInQueue": 12}


def slskd_handler(position_body=7):
    def handle(req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        if path == "/api/v0/application":
            return httpx.Response(200, json={"version": {"current": "0.26.1", "full": "0.26.1.0"}})
        if path == "/api/v0/options":
            return httpx.Response(200, json={"directories": {"downloads": "/mediadata/soulseek/complete",
                                                             "incomplete": "/mediadata/soulseek/incomplete"},
                                             "remoteFileManagement": True, "web": {"authentication": {"apiKeys": {"k": {"key": "SECRET"}}}}})
        if path == "/api/v0/searches" and method == "POST":
            return httpx.Response(200, json={"id": json.loads(req.content)["id"], "state": "InProgress"})
        if path.startswith("/api/v0/searches/") and path.endswith("/responses"):
            return httpx.Response(200, json=[{"username": "alice", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 900000,
                                              "files": [{"filename": r"@@music\Radiohead\OK Computer\01.flac", "size": 1, "bitDepth": 16,
                                                         "sampleRate": 44100, "length": 284}]}])
        if path.startswith("/api/v0/searches/") and method == "GET":
            return httpx.Response(200, json={"isComplete": True, "state": "Completed, TimedOut", "responseCount": 1, "fileCount": 1})
        if path.startswith("/api/v0/searches/") and method == "DELETE":
            return httpx.Response(204)
        if path == "/api/v0/transfers/downloads":
            return httpx.Response(200, json=[{"username": "alice", "directories": [{"directory": "x", "files": [TRANSFER]}]}])
        if path == "/api/v0/transfers/downloads/batches/b-1":
            return httpx.Response(200, json={"id": "b-1", "options": {"destination": "picker/12"}, "transfers": [TRANSFER]})
        if path.endswith("/position"):
            return httpx.Response(200, json=position_body)
        return httpx.Response(404)
    return handle


def lidarr_handler(write_tags="no", folder="{Album Title} ({Release Year})"):
    album = {"id": 9, "title": "OK Computer", "foreignAlbumId": "rg", "releaseDate": "1997-05-21T00:00:00Z",
             "releases": [{"foreignReleaseId": "r1", "monitored": True}, {"foreignReleaseId": "r2", "monitored": False}],
             "statistics": {"trackFileCount": 12, "trackCount": 12}}

    def handle(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/system/status":
            return httpx.Response(200, json={"version": "3.1.6.4900"})
        if path == "/wanted/missing":
            return httpx.Response(200, json={"page": 1, "pageSize": 5, "totalRecords": 1, "records": [
                {**album, "artist": {"artistName": "Radiohead", "foreignArtistId": "a"}}]})
        if path == "/album/9":
            return httpx.Response(200, json=album)
        if path == "/trackfile":
            return httpx.Response(200, json=[{"path": "/mediadata/media/music/Radiohead/OK Computer (1997)/01 - Airbag.flac"}])
        if path == "/config/metadataprovider":
            return httpx.Response(200, json={"writeAudioTags": write_tags, "scrubAudioTags": False})
        if path == "/config/naming":
            return httpx.Response(200, json={"renameTracks": True, "replaceIllegalCharacters": True, "colonReplacementFormat": 4,
                                             "standardTrackFormat": f"{folder}/{{track:00}} - {{Track Title}}",
                                             "multiDiscTrackFormat": f"{folder}/{{medium:0}}{{track:00}} - {{Track Title}}",
                                             "artistFolderFormat": "{Artist Name}"})
        if path == "/rootfolder":
            return httpx.Response(200, json=[{"path": "/mediadata/media/music/"}])
        if path == "/command":
            return httpx.Response(200, json=[{"id": 1, "name": "RescanFolders", "status": "completed"}])
        return httpx.Response(404)
    return handle


def plex_handler(location="/mediadata/media/music"):
    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"MediaContainer": {"Directory": [
            {"key": "3", "type": "artist", "title": "Music", "Location": [{"path": location}]}]}})
    return handle


class Services:
    def __init__(self, slskd, lidarr, plex):
        self.slskd = type("S", (), {"http": httpx.AsyncClient(transport=httpx.MockTransport(slskd), base_url="http://s/api/v0")})()
        self.lidarr = type("L", (), {"http": httpx.AsyncClient(transport=httpx.MockTransport(lidarr), base_url="http://l/api/v1")})()
        self.plex = type("P", (), {"http": httpx.AsyncClient(transport=httpx.MockTransport(plex), base_url="http://p")})()


@pytest.fixture
def configured(settings, monkeypatch):
    settings.download_dir = settings.download_dir.__class__("/mediadata/soulseek/complete")
    settings.music_dir = settings.music_dir.__class__("/mediadata/media/music")
    settings.slskd_url, settings.slskd_api_key = "http://s", "k"
    settings.lidarr_url, settings.lidarr_api_key = "http://l", "k"
    settings.plex_url, settings.plex_token, settings.plex_section_id = "http://p", "t", "3"
    settings.lidarr_root_folder = "/mediadata/media/music"
    monkeypatch.setattr(probe.asyncio, "sleep", _no_sleep)
    return settings


async def _no_sleep(_):
    return None


async def test_matching_setup_has_no_mismatches(configured, monkeypatch):
    monkeypatch.setattr(probe, "services", lambda: Services(slskd_handler(), lidarr_handler(), plex_handler()))
    probe.save_webhook(configured, {"eventType": "download", "album": {"id": 9, "mbId": "rg"},
                                    "trackFiles": [{"path": "/mediadata/media/music/Radiohead/OK Computer (1997)/01 - Airbag.flac"}]})
    report = await probe.run_probe(configured)
    bad = [f for f in report.findings if f.level in ("mismatch", "error")]
    assert bad == [], report.text()
    assert (report.out_dir / "report.txt").exists()
    assert (report.out_dir / "slskd_batch.json").exists()
    assert "SECRET" not in (report.out_dir / "slskd_options.json").read_text()  # scrubbed before saving
    assert "folder part matches album_folder_template" in report.text()


async def test_misconfiguration_is_reported(configured, monkeypatch):
    monkeypatch.setattr(probe, "services", lambda: Services(
        slskd_handler(position_body={"position": 3}),
        lidarr_handler(write_tags="newFiles", folder="{Artist Name} - {Album Title}"),
        plex_handler(location="/data/music"),
    ))
    text = (await probe.run_probe(configured)).text()
    assert "Write Audio Tags = newFiles" in text
    assert "differs from album_folder_template" in text
    assert "queue position reply is dict" in text
    assert "Plex library folders ['/data/music']" in text


def test_get_path_and_scrub():
    data = {"records": [{"artist": {"name": "a"}}, {"artist": {"name": "b"}}], "apiKey": "x", "nested": {"Password": "p"}}
    assert probe.get_path(data, "records[].artist.name") == ["a", "b"]
    assert probe.scrub(data)["apiKey"] == "<redacted>" and probe.scrub(data)["nested"]["Password"] == "<redacted>"


def test_webhook_files_are_rotated(settings):
    for i in range(probe.KEEP_WEBHOOKS + 5):
        probe.save_webhook(settings, {"eventType": "Test", "n": i})
    assert len(list((settings.config_dir / "probe" / "webhooks").glob("*.json"))) == probe.KEEP_WEBHOOKS
