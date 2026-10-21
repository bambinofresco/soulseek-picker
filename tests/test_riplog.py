"""Rip log TOC parsing and disc IDs, checked against MusicBrainz's own disc ID records.

The fixture logs are built by tests/make_log_fixtures.py from real MusicBrainz TOCs, so
the expected IDs are MusicBrainz's. When the `discid` package and libdiscid are installed,
discid.put() is used as a second, independent check.
"""

from pathlib import Path

import pytest
import yaml

from picker.riplog import TocEntry, decode, disc_id, parse_cue, parse_log, parse_tocs

LOGS = Path(__file__).parent / "fixtures" / "logs"
EXPECTED = yaml.safe_load((LOGS / "expected.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_disc_ids_match_musicbrainz(name):
    info = parse_log((LOGS / name).read_bytes())
    assert info.parsed
    assert [d.id for d in info.disc_ids] == EXPECTED[name]
    assert all(len(d.id) == 28 for d in info.disc_ids)


def test_eac_utf16_details():
    data = (LOGS / "eac_utf16.log").read_bytes()
    assert data.startswith(b"\xff\xfe") and b"\r\x00\n\x00" in data  # UTF-16LE BOM, CRLF
    info = parse_log(data)
    assert (info.ripper, info.read_mode, info.secure) == ("EAC", "Secure", True)
    assert (info.accurate, info.crc_checked, info.crc_mismatches, info.errors, info.checksum) == (12, 12, 0, False, True)
    assert info.verdict == "good"
    assert info.disc_ids[0].toc_param.startswith("1 12 240687 150 21480")


def test_bad_rip_is_flagged():
    info = parse_log((LOGS / "eac_errors.log").read_bytes())
    assert info.secure is False and info.crc_mismatches == 1 and info.errors is True
    assert info.verdict == "bad"


def test_enhanced_cd_drops_the_data_track():
    info = parse_log((LOGS / "eac_enhanced_cd.log").read_bytes())
    d = info.disc_ids[0]
    assert d.data_track_dropped and d.last == 12 and d.leadout == 241038
    assert info.tracks == 13  # the TOC lists 13; the disc ID covers 12


def test_two_disc_xld():
    info = parse_log((LOGS / "xld_two_disc.log").read_bytes())
    assert info.ripper == "XLD" and info.secure and info.checksum
    assert len(parse_tocs(decode((LOGS / "xld_two_disc.log").read_bytes()))) == 2


def test_other_rippers_are_present_but_not_parsed():
    info = parse_log(b"CUERipper v2.2.5 Copyright (C) 2008-2023 Grigory Chudov\n\nTOC of the extracted CD\n")
    assert info.ripper == "CUERipper" and not info.parsed and info.verdict == "unknown"
    assert parse_log(b"just some text").ripper is None


def test_decoding_fallbacks():
    assert decode("Exact Audio Copy".encode("utf-16-le")) == "Exact Audio Copy"  # no BOM
    assert decode("Björk".encode("latin-1")) == "Björk"
    assert decode("﻿XLD".encode("utf-8")) == "XLD"


def test_disc_id_slots_follow_track_numbers():
    # A disc whose first track is 1 and which has no data track: the plain case.
    toc = [TocEntry(1, 0, 999), TocEntry(2, 1000, 1999)]
    d = disc_id(toc)
    assert (d.first, d.last, d.leadout, d.offsets) == (1, 2, 2150, [150, 1150])


def test_cue():
    cue = parse_cue((LOGS / "album.cue").read_bytes())
    assert cue.catalog == "0724385522925"
    assert cue.isrcs == {1: "GBAYE9700323", 2: "GBAYE9700324"}
    assert parse_cue(b'CATALOG 0000000000000\n').catalog is None


def test_independent_check_with_libdiscid():
    discid = pytest.importorskip("discid")
    try:
        for name, ids in EXPECTED.items():
            for d, want in zip(parse_log((LOGS / name).read_bytes()).disc_ids, ids):
                assert discid.put(d.first, d.last, d.leadout, d.offsets).id == want
    except OSError:
        pytest.skip("libdiscid not available")
