"""The recommendation card (picker/recommend.py)."""

from datetime import datetime, timedelta, timezone

from picker import recommend
from picker.config import Settings
from picker.db import session
from picker.editions import Edition
from picker.models import Candidate, Download

GB = Edition(id="gb", title="OK Computer", date="1997-06-16", country="GB", formats=["CD"])
JP = Edition(id="jp", title="OK Computer", date="1997-05-28", country="JP", formats=["CD"])
RM = Edition(id="rm", title="OK Computer", date="2009-03-24", country="GB", formats=["CD"], disambiguation="remastered")
EDITIONS = {e.id: e for e in (GB, JP, RM)}


def cand(username, tier="Exact", editions=("gb",), signals=(), **kw) -> Candidate:
    defaults = dict(search_id=1, folder=rf"m\\{username}\\OK Computer", query="q", files=[], audio_count=12, disc_counts=[12],
                    total_size=360_000_000, audio_format="FLAC", bit_depth=16, sample_rate=44100, sidecars={"image": 1},
                    has_free_slot=True, queue_length=0, upload_speed=2_400_000, parsed={"source": "CD"},
                    match={"tier": tier, "editions": list(editions), "tracks_agree": True,
                           "signals": [{"name": n, "weight": 1, "detail": n} for n in signals]})
    defaults.update(kw)
    return Candidate(username=username, **defaults)


def make_settings(**kw) -> Settings:
    return Settings(config_dir="/tmp/x", download_dir="/tmp/d", music_dir="/tmp/m", slskd_api_key="k", **kw)


def test_estimate_uses_queue_and_speed():
    fast = recommend.estimate(cand("a"))
    assert fast.seconds == 360_000_000 / 2_400_000 and fast.short == "~3 min" and fast.detail == "free slot, 2.4 MB/s"
    queued = recommend.estimate(cand("b", has_free_slot=False, queue_length=30))
    assert queued.queue_seconds == 30 * recommend.QUEUED_FILE_BYTES / 2_400_000 and "(queue)" in queued.short
    slow = recommend.estimate(cand("c", upload_speed=0))
    assert slow.speed == recommend.SLOW_BPS and "speed unknown" in slow.detail and slow.seconds > fast.seconds
    assert recommend.estimate(cand("d", upload_speed=0, has_free_slot=False, queue_length=0)).seconds is None
    measured = recommend.estimate(cand("e", upload_speed=100_000), {"e": 5_000_000})
    assert measured.measured and "5.0 MB/s measured" in measured.detail


def test_measured_speed_from_this_sessions_downloads(settings):
    now = datetime.now(timezone.utc)
    with session() as s:
        s.add(Download(candidate_id=1, search_id=1, rg_mbid="rg", username="alice", folder="f", destination="picker/1", files=[],
                       state="completed", bytes_done=300_000_000, bytes_total=300_000_000,
                       created_at=now - timedelta(seconds=100), finished_at=now))
        s.add(Download(candidate_id=2, search_id=1, rg_mbid="rg", username="bob", folder="f", destination="picker/2", files=[],
                       state="failed", bytes_done=300_000_000, created_at=now - timedelta(seconds=100)))
        s.add(Download(candidate_id=3, search_id=1, rg_mbid="rg", username="carol", folder="f", destination="picker/3", files=[],
                       state="completed", bytes_done=1_000_000, created_at=now - timedelta(seconds=100), finished_at=now))
        s.commit()
    speeds = recommend.measured_speeds({"alice", "bob", "carol", "dave"})
    assert set(speeds) == {"alice"} and 2_900_000 < speeds["alice"] <= 3_000_000


def test_recommendation_order_and_reasons(settings):
    s = make_settings()
    alice = cand("alice", signals=("disc_id",), sidecars={"log": 1, "image": 1},
                 log_info={"state": "done", "logs": [{"secure": True, "accurate": 12, "crc_checked": 12, "crc_mismatches": 0, "verdict": "good"}]})
    bob = cand("bob", signals=("catalog",), editions=("gb", "jp"), has_free_slot=False, queue_length=7, sidecars={})
    carol = cand("carol", tier="High", editions=("rm",), audio_format="MP3", parsed={"source": "WEB", "remaster": "folder"})
    dave = cand("dave", tier="Low", audio_count=13, match={"tier": "Low", "editions": ["gb"], "tracks_agree": False, "signals": []})
    erin = cand("erin", tier="Unknown", match={"tier": "Unknown"})
    for i, c in enumerate((alice, bob, carol, dave, erin), 1):
        c.id = i
    rec = recommend.recommend([dave, carol, bob, alice, erin], EDITIONS, s)
    assert rec.best.candidate is alice and rec.best.verification == "disc ID" and rec.best.edition is GB
    assert rec.best.summary[0] == "Exact by disc ID" and "verified log, AccurateRip 12 ok" in rec.best.summary
    assert "complete" in rec.best.summary and "cover art" in rec.best.summary and rec.best.summary[-1].endswith("about 3 min")
    assert rec.runner_up.candidate is bob
    why = dict((c.username, r) for c, r in rec.why_not)
    assert "one of 2 editions" in why["bob"] and "no log" in why["bob"] and "queue of 7" in why["bob"]
    assert why["carol"].startswith("High match") and "MP3" in why["carol"] and "remaster" in why["carol"]
    assert why["dave"].startswith("Low match") and "13 tracks" in why["dave"]
    assert "erin" not in why  # nothing matched: never recommended
    assert recommend.recommend([erin], EDITIONS, s) is None
    assert recommend.recommend([], EDITIONS, s) is None


def test_ties_break_on_preferences_then_time_then_size(settings):
    s = make_settings()
    gb = cand("gb-user", editions=("gb",), total_size=400_000_000)
    jp = cand("jp-user", editions=("jp",))
    slow = cand("slow", editions=("gb",), upload_speed=200_000)
    small = cand("small", editions=("gb",), total_size=350_000_000)
    for i, c in enumerate((gb, jp, slow, small), 1):
        c.id = i
    rec = recommend.recommend([jp, slow, gb, small], EDITIONS, s)
    # Preferences rank before the time estimate: the slow GB folder still beats the fast JP one.
    assert [p.username for p, _ in rec.why_not] == ["gb-user", "slow", "jp-user"] and rec.best.candidate is small
    why = dict((c.username, r) for c, r in rec.why_not)
    assert why["gb-user"] == "same on every count; bigger"
    assert "slower" in why["slow"]
    assert "− " not in why["jp-user"] and "preferences" in why["jp-user"]
