"""Rip verification (picker/ripverify.py): checksums against brute-force definitions, the
database formats, and the whole check with synthetic audio and fake lookups."""

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
from mutagen.flac import FLAC

from picker import ripverify as rv
from picker.config import Settings
from picker.riplog import TocEntry

# whipper's test vector: 12 audio tracks plus a data track (CD-Extra).
LADYHAWKE = [0, 15537, 31691, 50866, 66466, 81202, 99409, 115920, 133093, 149847, 161560, 177682, 207106]
LADYHAWKE_LEADOUT = 210385


def make_cd_flac(path: Path, samples: int) -> None:
    """A FLAC header claiming `samples` samples of 16-bit 44.1 kHz stereo (no frames; decoding is faked)."""
    info = (44100 << 44) | (1 << 41) | (15 << 36) | samples
    block = struct.pack(">HH", 4096, 4096) + b"\0" * 6 + struct.pack(">Q", info) + b"\0" * 16
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + block)
    f = FLAC(path)
    f["title"] = path.stem
    f.save()


def settings(**kw) -> Settings:
    return Settings(config_dir="/tmp/x", download_dir="/tmp/d", music_dir="/tmp/m", slskd_api_key="k", **kw)


def rand_words(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 2**32, size=n, dtype=np.uint64).astype(np.uint32)


# TOC and IDs ----------------------------------------------------------------------------------

def test_accuraterip_ids_match_whippers_test_vector():
    toc = rv.Toc(LADYHAWKE, [True] * 12 + [False], LADYHAWKE_LEADOUT, "log")
    assert rv.ar_ids(toc) == (0x0013BD5A, 0x00B8D489, 0xC60AF50D)
    assert rv.ar_path(toc) == "a/5/d/dBAR-012-0013bd5a-00b8d489-c60af50d.bin"
    assert toc.ctdb_string() == "0:15537:31691:50866:66466:81202:99409:115920:133093:149847:161560:177682:-207106:210385"
    assert toc.audio_leadout == 207106 - 11400 and len(toc.audio_sectors) == 12


def test_toc_from_log_marks_a_cd_extra_data_track():
    rows = [TocEntry(i + 1, s, e - 1) for i, (s, e) in enumerate(zip(LADYHAWKE, LADYHAWKE[1:] + [LADYHAWKE_LEADOUT]))]
    rows[-2] = TocEntry(12, 177682, 207106 - 11400 - 1)  # the audio ends before the session gap
    toc = rv.toc_from_log(rows)
    assert toc.audio == [True] * 12 + [False] and toc.leadout == LADYHAWKE_LEADOUT and toc.source == "log"
    plain = rv.toc_from_log(rows[:12])
    assert plain.audio == [True] * 12 and plain.leadout == 207106 - 11400


def test_toc_from_lengths_rounds_up_to_sectors_and_strips_4608():
    toc = rv.toc_from_lengths([588 * 100, 588 * 50 + 1, 588 * 20 + 4608])  # 4608 extra samples leave a remainder of 492
    assert toc.offsets == [0, 100, 151] and toc.leadout == 151 + 20 and toc.source == "lengths"
    assert rv.toc_from_lengths([]) is None and rv.toc_from_lengths([588, 0]) is None


# Checksums against plain definitions ----------------------------------------------------------

def brute_v1_v2(words, first, last):
    n = len(words)
    lo = 2939 if first else 0
    hi = n - 2940 if last else n
    v1 = v2 = 0
    for i in range(lo, hi):
        prod = int(words[i]) * (i + 1)
        v1 += prod & 0xFFFFFFFF
        v2 += prod >> 32
    v1 &= 0xFFFFFFFF
    return v1, (v1 + v2) & 0xFFFFFFFF


def test_ar_checksums_match_the_definition():
    words = rand_words(588 * 12, 1)
    for first, last in ((True, False), (False, False), (False, True), (True, True)):
        assert rv.ar_checksums(words, first, last) == brute_v1_v2(words, first, last)
    assert rv.ar_checksums(words[:100], True, True) == (0, 0)


def test_frame450_sums_for_every_offset_match_the_definition():
    prev, words, nxt = rand_words(2939, 2), rand_words(588 * 460, 3), rand_words(2939, 4)
    sums = rv.frame450_all_offsets(words, prev, nxt)
    assert len(sums) == 2 * 2939 + 1
    disc = np.concatenate([prev, words, nxt])
    for oi in (0, 1, -1, 7, -2939, 2939, 1234):
        a = 2939 + 450 * 588 + oi
        expect = sum(int(disc[a + k]) * (k + 1) for k in range(588)) & 0xFFFFFFFF
        assert sums[oi] == expect, oi
    assert rv.frame450_all_offsets(words[:588 * 400], prev, nxt) == {}  # too short for frame 450


def test_shifted_window_borrows_from_the_neighbours_and_pads_with_zeros():
    prev, words, nxt = rand_words(2939, 5), rand_words(1000, 6), rand_words(2939, 7)
    assert rv.shifted(words, prev, nxt, 0) is words
    plus = rv.shifted(words, prev, nxt, 10)
    assert np.array_equal(plus[:990], words[10:]) and np.array_equal(plus[990:], nxt[:10])
    minus = rv.shifted(words, prev, nxt, -10)
    assert np.array_equal(minus[:10], prev[-10:]) and np.array_equal(minus[10:], words[:990])
    far = rv.shifted(words, np.zeros(0, dtype=np.uint32), np.zeros(0, dtype=np.uint32), -5)
    assert len(far) == 1000 and not far[:5].any() and np.array_equal(far[5:], words[:995])


def test_ctdb_bounds():
    assert rv.ctdb_bounds(588 * 1000) == (5880, 5880)
    assert rv.ctdb_bounds(588 * 1003) == (5880, 5880 + 588 * 3)


# Database formats -----------------------------------------------------------------------------

def test_parse_accuraterip_records():
    rec = bytes([2]) + struct.pack("<III", 1, 2, 3) + bytes([35]) + struct.pack("<II", 0xAB, 0xCD) + bytes([3]) + struct.pack("<II", 0, 0)
    discs = rv.parse_ar(rec + rec)
    assert len(discs) == 2 and discs[0]["id1"] == 1 and discs[0]["tracks"][0] == {"confidence": 35, "crc": 0xAB, "crc450": 0xCD}
    assert rv.parse_ar(b"") == [] and rv.parse_ar(rec[:25])[0]["tracks"] == [{"confidence": 35, "crc": 0xAB, "crc450": 0xCD}]


CTDB_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<ctdb xmlns="http://db.cuetools.net/ns/mmd-1.0#">
<entry confidence="60" crc32="2896e394" hasparity="http://p.cuetools.net/756640" id="756640" npar="16" stride="5880"
       toc="0:15537:31691:-207106:210385" trackcrcs="ab2cd549 362bbe9d 47b2732c" />
<entry confidence="2" crc32="deadbeef" id="1" npar="8" stride="5880" toc="0:15537:31691:210385" trackcrcs="" />
<metadata source="musicbrainz" id="x" artist="A" album="B"><track name="t"/></metadata>
</ctdb>'''


def test_parse_ctdb_entries():
    entries = rv.parse_ctdb(CTDB_XML)
    assert [e["confidence"] for e in entries] == [60, 2]
    assert entries[0]["crc32"] == 0x2896E394 and entries[0]["trackcrcs"] == [0xAB2CD549, 0x362BBE9D, 0x47B2732C] and entries[0]["hasparity"]
    assert entries[1]["trackcrcs"] == [] and not entries[1]["hasparity"]
    assert rv.parse_ctdb("Not Found") == []


# The whole check ------------------------------------------------------------------------------

def disc_from(words_per_track: list[np.ndarray], toc: rv.Toc, offset: int = 0):
    """What the databases would hold for these tracks, ripped by a drive `offset` samples off."""
    disc = np.concatenate(words_per_track)
    if offset:
        disc = np.concatenate([disc[offset:], np.zeros(offset, dtype=np.uint32)]) if offset > 0 else \
            np.concatenate([np.zeros(-offset, dtype=np.uint32), disc[:offset]])
    starts = [0]
    for w in words_per_track[:-1]:
        starts.append(starts[-1] + len(w))
    ends = starts[1:] + [len(disc)]
    n = len(words_per_track)
    ar_tracks = []
    for t in range(n):
        tw = disc[starts[t]:ends[t]]
        v1, v2 = brute_v1_v2(tw, t == 0, t == n - 1)
        f450 = sum(int(disc[starts[t] + 450 * 588 + k]) * (k + 1) for k in range(588)) & 0xFFFFFFFF if len(tw) >= 451 * 588 else 0
        ar_tracks.append({"confidence": 20, "crc": v1, "crc450": f450})
    prefix, suffix = rv.ctdb_bounds(len(disc))
    disc_crc = zlib.crc32(disc[prefix:len(disc) - suffix].astype("<u4").tobytes())
    track_crcs = []
    for t in range(n):
        a = starts[t] + (prefix if t == 0 else 0)
        b = ends[t] - (suffix if t == n - 1 else 0)
        track_crcs.append(zlib.crc32(disc[a:b].astype("<u4").tobytes()))
    ar = [{"id1": 1, "id2": 2, "cddb": 3, "tracks": ar_tracks}]
    ctdb = [{"id": "1", "crc32": disc_crc, "confidence": 40, "trackcrcs": track_crcs, "toc": toc.ctdb_string(), "hasparity": True}]
    return ar, ctdb


@pytest.fixture
def album(tmp_path):
    folder = tmp_path / "album"
    folder.mkdir()
    sectors = [470, 455, 460]
    words = [rand_words(s * 588, 10 + i) for i, s in enumerate(sectors)]
    files = []
    for i, w in enumerate(words, 1):
        path = folder / f"{i:02d} Track.flac"
        make_cd_flac(path, len(w))
        files.append(path)
    toc = rv.toc_from_lengths([len(w) for w in words])
    return folder, files, words, toc


def fakes(words, files, ar, ctdb, calls=None):
    by_name = {f.name: w for f, w in zip(files, words)}

    async def decode(path):
        return by_name[Path(path).name]

    async def fetch_ar(toc, s):
        if calls is not None:
            calls.append(("ar", toc.ctdb_string()))
        return ar

    async def fetch_ctdb(toc, s):
        if calls is not None:
            calls.append(("ctdb", toc.ctdb_string()))
        return ctdb

    return dict(decode=decode, fetch_ar=fetch_ar, fetch_ctdb=fetch_ctdb)


async def test_a_clean_rip_verifies_in_both_databases(album):
    folder, files, words, toc = album
    ar, ctdb = disc_from(words, toc)
    calls = []
    result = await rv.verify_folder(folder, files, settings(), **fakes(words, files, ar, ctdb, calls))
    disc = result["discs"][0]
    assert result["applicable"] and disc["toc_source"] == "lengths" and calls == [("ar", toc.ctdb_string()), ("ctdb", toc.ctdb_string())]
    assert disc["accuraterip"]["status"] == "verified" and disc["accuraterip"]["offset"] == 0 and disc["accuraterip"]["confidence"] == 20
    assert [r["status"] for r in disc["accuraterip"]["tracks"]] == ["ok", "ok", "ok"]
    assert disc["ctdb"]["status"] == "verified" and disc["ctdb"]["confidence"] == 40 and disc["ctdb"]["same_toc"] == 1
    assert [r["status"] for r in disc["ctdb"]["tracks"]] == ["ok", "ok", "ok"]
    assert result["mismatched"] == [] and rv.verdict(result) == "verified"
    assert result["summary"] == "CTDB verified (confidence 40), AccurateRip 3/3 (confidence 20)"


async def test_one_damaged_track_is_called_out(album):
    folder, files, words, toc = album
    ar, ctdb = disc_from(words, toc)
    damaged = [w.copy() for w in words]
    damaged[1][100_000] ^= 1  # one flipped bit, deep inside track 2
    result = await rv.verify_folder(folder, files, settings(), **fakes(damaged, files, ar, ctdb))
    disc = result["discs"][0]
    assert [r["status"] for r in disc["accuraterip"]["tracks"]] == ["ok", "mismatch", "ok"]
    assert disc["accuraterip"]["status"] == "mismatch" and disc["ctdb"]["status"] == "mismatch"
    assert [r["status"] for r in disc["ctdb"]["tracks"]] == ["ok", "mismatch", "ok"]
    assert result["mismatched"] == ["02 Track.flac"] and rv.verdict(result) == "mismatch"
    assert result["summary"] == "CTDB 2/3 tracks match, AccurateRip 2/3 tracks accurate"


async def test_a_pressing_offset_is_found_and_both_databases_verify(album):
    folder, files, words, toc = album
    ar, ctdb = disc_from(words, toc, offset=30)  # the database rips came from a drive 30 samples off
    result = await rv.verify_folder(folder, files, settings(), **fakes(words, files, ar, ctdb))
    disc = result["discs"][0]
    assert disc["accuraterip"]["status"] == "verified" and disc["accuraterip"]["offset"] == 30
    assert disc["ctdb"]["status"] == "verified" and disc["ctdb"]["offset"] == 30
    assert "offset +30" in result["summary"]


async def test_not_in_either_database_and_lookup_failures(album):
    folder, files, words, toc = album
    result = await rv.verify_folder(folder, files, settings(), **fakes(words, files, [], []))
    disc = result["discs"][0]
    assert disc["accuraterip"]["status"] == "not_in_db" and disc["ctdb"]["status"] == "not_in_db"
    assert rv.verdict(result) == "not_in_db" and result["summary"] == "CTDB: not in database, AccurateRip: not in database"
    result = await rv.verify_folder(folder, files, settings(), **fakes(words, files, None, None))
    assert result["discs"][0]["accuraterip"]["status"] == "error" and "lookup failed" in result["summary"]
    result = await rv.verify_folder(folder, files, settings(accuraterip=False), **fakes(words, files, [], []))
    assert result["discs"][0]["accuraterip"]["status"] == "off" and "AccurateRip off" in result["summary"]


async def test_a_log_toc_is_used_when_it_fits_the_files(album):
    folder, files, words, toc = album
    sectors = toc.audio_sectors
    rows, start = [], 0
    for i, s in enumerate(sectors, 1):
        rows.append(f"{i:>9} | 0:00.00 | 0:00.00 | {start:>12} | {start + s - 1:>10}")
        start += s
    (folder / "rip.log").write_text("Exact Audio Copy V1.6\n\nTOC of the extracted CD\n\n     Track |   Start  |  Length  | Start sector | End sector \n"
                                    "    ---------------------------------------------------------\n" + "\n".join(rows) + "\n\nRange status\n", encoding="utf-8")
    ar, ctdb = disc_from(words, toc)
    result = await rv.verify_folder(folder, files, settings(), **fakes(words, files, ar, ctdb))
    assert result["discs"][0]["toc_source"] == "log" and rv.verdict(result) == "verified"


async def test_non_cd_audio_is_not_applicable(tmp_path):
    folder = tmp_path / "hires"
    folder.mkdir()
    path = folder / "01.flac"
    info = (96000 << 44) | (1 << 41) | (23 << 36) | 96000 * 10
    block = struct.pack(">HH", 4096, 4096) + b"\0" * 6 + struct.pack(">Q", info) + b"\0" * 16
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + block)
    result = await rv.verify_folder(folder, [path], settings())
    assert not result["applicable"] and rv.verdict(result) == "not_applicable"
    assert result["summary"] == "not CD audio (16-bit 44.1 kHz stereo)"
    assert rv.verdict(None) == "none"


def test_candidate_offsets_vote_across_tracks():
    f450 = [{0: 1, 30: 7}, {0: 2, 30: 8, -5: 9}, {}]
    records = [{"tracks": [{"crc450": 7}, {"crc450": 8}, {"crc450": 0}]}, {"tracks": [{"crc450": 1}, {"crc450": 9}, {"crc450": 0}]}]
    assert rv.candidate_offsets(f450, records) == {30}
    assert rv.candidate_offsets(f450, []) == set()
