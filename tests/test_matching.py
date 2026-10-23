import pytest

from picker.editions import edition_from_release, editions_from_releases, format_family
from picker.matching import match_candidate, rip_quality
from picker.parsing import parse_candidate

LENGTHS = [284, 383, 284, 323, 216, 264, 107, 257, 228, 249, 278, 268]  # seconds


def release(rid, date, country, fmt="CD", label="Parlophone", cat="", barcode="", disamb="", lengths=LENGTHS,
            media=None, discids=()):
    media = media or [(fmt, lengths)]
    return {
        "id": rid, "title": "OK Computer", "date": date, "country": country, "disambiguation": disamb,
        "status": "Official", "barcode": barcode,
        "label-info": [{"label": {"name": label}, "catalog-number": cat}] if label else [],
        "media": [
            {"position": i + 1, "format": f, "track-count": len(ls),
             "tracks": [{"length": int(x * 1000) if x else None} for x in ls],
             "discs": [{"id": d} for d in discids] if i == 0 else []}
            for i, (f, ls) in enumerate(media)
        ],
    }


RELEASES = [
    release("uk-cd", "1997-05-21", "GB", cat="NODATA 02", barcode="724385522925"),
    release("us-cd", "1997-07-01", "US", label="Capitol Records", cat="CDP 7243 8 55229 2 5", barcode="724385522925"),
    release("jp-cd", "1997-05-28", "JP", label="Toshiba-EMI", cat="TOCP-50201", barcode="4988006719743"),
    release("uk-lp", "1997-06-16", "GB", fmt='12" Vinyl', cat="NODATA 02LP"),
    release("rm-2009", "2009-03-24", "GB", cat="5099969945626", disamb="2009 collector's edition, remastered",
            media=[("CD", LENGTHS), ("CD", [200.0] * 11), ("DVD-Video", [100.0] * 8)]),
    release("web", "2016-01-01", "XW", fmt="Digital Media", label="XL Recordings", cat=""),
]
EDITIONS = editions_from_releases(RELEASES)


def folder(path, lengths=LENGTHS, discs=None, extra=(), labels=("Parlophone", "Capitol Records", "Toshiba-EMI", "XL Recordings")):
    """Candidate files plus its parse, as the search job would build them."""
    files = []
    for disc, lens in (discs or [(None, lengths)]):
        for i, length in enumerate(lens, 1):
            sub = f"{disc}\\" if disc else ""
            files.append({"filename": rf"{path}\{sub}{i:02d} Track.flac", "kind": "audio", "disc": disc,
                          "length": length, "size": 1})
    files += [{"filename": rf"{path}\{x}", "kind": "log" if x.endswith(".log") else "image", "disc": None} for x in extra]
    parsed = parse_candidate([f["filename"] for f in files], files=files, artist="Radiohead", album="OK Computer",
                             labels=list(labels)).to_dict()
    return files, parsed


def match(path, **kw):
    disc_ids = kw.pop("disc_id_releases", frozenset())
    files, parsed = folder(path, **kw)
    return match_candidate(files, parsed, EDITIONS, disc_id_releases=disc_ids)


def test_editions_are_sorted_and_summarised():
    assert [e.id for e in EDITIONS][:3] == ["uk-cd", "jp-cd", "uk-lp"]
    rm = next(e for e in EDITIONS if e.id == "rm-2009")
    assert rm.format_summary == "2×CD + DVD-Video"
    assert rm.audio_media == [0, 1]
    assert rm.is_remaster
    assert rm.year == 2009
    assert format_family('12" Vinyl') == "Vinyl"
    assert format_family("Hybrid SACD") == "SACD"
    assert format_family("Digital Media") == "WEB"
    assert format_family("SHM-CD") == "CD"


@pytest.mark.parametrize("path, edition", [
    (r"m\Radiohead\OK Computer [CDP 7243 8 55229 2 5]", "us-cd"),
    (r"m\Radiohead\OK Computer [Japan TOCP-50201]", "jp-cd"),
    (r"m\Radiohead\OK Computer (1997) [NODATA 02]", "uk-cd"),
])
def test_catalog_number_in_path_is_exact(path, edition):
    """Phase 3 acceptance: a folder carrying a catalog number matches that edition at Exact."""
    result = match(path)
    assert result.tier == "Exact"
    assert result.editions == [edition]
    assert result.matched_catalogs


def test_barcode_is_exact():
    result = match(r"m\Radiohead\OK Computer [4988006719743]")
    assert (result.tier, result.editions) == ("Exact", ["jp-cd"])


def test_shared_barcode_stays_ambiguous_and_ignores_leading_zero():
    result = match(r"m\Radiohead\OK Computer [0724385522925]")  # EAN-13 form of the UPC
    assert result.tier == "Exact"
    assert result.editions == ["uk-cd", "us-cd"]  # one of 2, earliest first


def test_indistinguishable_cds_form_an_ambiguity_set():
    result = match(r"m\Radiohead\OK Computer [CD]")
    assert result.tier == "High"  # tracks + durations + source
    assert set(result.editions) == {"uk-cd", "us-cd", "jp-cd"}
    assert result.earliest_date == "1997-05-21"
    assert len(result.track_diffs) == 12 and result.track_diffs[0]["diff"] == 0


def test_label_narrows_the_set():
    result = match(r"m\Radiohead\OK Computer [Toshiba-EMI] [CD]")
    assert result.editions == ["jp-cd"]
    assert result.tier == "High"


def test_no_hints_is_medium_with_durations():
    result = match(r"m\Radiohead\OK Computer")
    assert result.tier == "Medium"
    assert "web" in result.editions  # digital has the same tracks; nothing rules it out


def test_track_count_mismatch_caps_at_low():
    result = match(r"m\Radiohead\OK Computer [NODATA 02]", lengths=LENGTHS[:10])
    assert result.tier == "Low"
    assert not result.tracks_agree


def test_nothing_usable_is_unknown():
    assert match(r"m\Radiohead\OK Computer", lengths=LENGTHS[:5]).tier == "Unknown"


def test_source_contradiction_is_low_for_cds_but_vinyl_edition_wins():
    result = match(r"m\Radiohead\OK Computer [Vinyl Rip 24-96]")
    assert result.editions == ["uk-lp"]
    assert result.tier == "High"


def test_bad_durations_are_a_contradiction():
    off = [x + 30 for x in LENGTHS]
    result = match(r"m\Radiohead\OK Computer [CD]", lengths=off)
    assert result.tier == "Low"


def test_missing_lengths_skip_durations():
    lengths = [None] * 4 + LENGTHS[4:]
    result = match(r"m\Radiohead\OK Computer [CD]", lengths=lengths)
    assert result.tier == "Medium"  # tracks + source, no durations signal
    assert not any(s.name.startswith("durations") for s in result.signals)


def test_remaster_flag_prefers_the_remaster_edition():
    result = match(r"m\Radiohead\OK Computer (2009 Remaster)",
                   discs=[("CD1", LENGTHS), ("CD2", [200.0] * 11)])
    assert result.editions == ["rm-2009"]
    assert result.tracks_agree  # the DVD-Video medium is ignored
    assert result.tier in ("High", "Medium")


def test_flat_folder_holding_two_discs():
    files_lengths = LENGTHS + [200.0] * 11
    result = match(r"m\Radiohead\OK Computer (2009 Remaster)", lengths=files_lengths)
    assert result.editions == ["rm-2009"] and result.tracks_agree


def test_disc_id_is_definitive():
    result = match(r"m\Radiohead\OK Computer", disc_id_releases={"jp-cd"})
    assert (result.tier, result.editions) == ("Exact", ["jp-cd"])


def test_no_editions():
    files, parsed = folder(r"m\Radiohead\OK Computer")
    assert match_candidate(files, parsed, []).tier == "Unknown"


def test_rip_quality():
    assert rip_quality({"log": 1, "cue": 1}, {"rip_tool": "EAC"})["score"] == 3
    assert rip_quality({}, {"rip_tool": "EAC"})["score"] == 0


def test_edition_from_release_reads_recording_lengths_and_discids():
    r = release("x", "2000", "GB", discids=["abc-"])
    r["media"][0]["tracks"][0] = {"length": None, "recording": {"length": 1500}}
    e = edition_from_release(r)
    assert e.lengths[0][0] == 1.5
    assert e.disc_ids == [["abc-"]]
    assert e.catalog_numbers == []  # empty catalog number dropped


def test_a_shared_catalog_number_does_not_beat_the_right_format():
    """Sufjan Stevens, The Greatest Gift: the WEB folder tagged [AKR134] is the digital release
    (no catalog number in MusicBrainz), not the AKR134 cassette or CD."""
    lengths = [412, 328, 248, 257, 121, 111, 327, 204, 408, 244, 134, 112]
    eds = editions_from_releases([
        release("digital", "2017-11-24", "XW", fmt="Digital Media", label="Asthmatic Kitty Records", cat="", lengths=lengths),
        release("cassette", "2017-11-24", "US", fmt="Cassette", label="Asthmatic Kitty Records", cat="AKR134", lengths=lengths),
        release("vinyl", "2017-12-08", "US", fmt='12" Vinyl', label="Asthmatic Kitty Records", cat="AKR134LP", lengths=lengths),
        release("cd", "2018-11-16", "US", fmt="CD", label="Asthmatic Kitty Records", cat="AKR134", lengths=lengths),
    ])
    path = r"@@ibnde\AUDIOGALORE\Backlog\Sufjan Stevens - The Greatest Gift Mixtape (2017) [AKR134] [WEB FLAC]"
    files = [{"filename": rf"{path}\{i:02d}.flac", "kind": "audio", "disc": None, "length": n, "size": 1}
             for i, n in enumerate(lengths, 1)]
    parsed = parse_candidate([f["filename"] for f in files], files=files, artist="Sufjan Stevens",
                             album="The Greatest Gift", labels=["Asthmatic Kitty Records"]).to_dict()
    assert parsed["source"] == "WEB" and parsed["catalog_numbers"] == ["AKR134"]
    result = match_candidate(files, parsed, eds)
    assert result.editions == ["digital"]
    assert result.tier == "High"  # tracks, lengths, year and source agree

    # The same folder ripped from CD still matches the CD/cassette catalog number exactly.
    cd_parsed = {**parsed, "source": "CD"}
    assert match_candidate(files, cd_parsed, eds).tier == "Exact"
