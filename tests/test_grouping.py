from picker.grouping import (
    availability_key,
    candidate_drafts,
    disc_number,
    file_kind,
    group_responses,
    passes_default_filter,
    place,
)


def f(filename, size=30_000_000, **attrs):
    return {"filename": filename, "size": size, **attrs}


def response(username, files, free=True, queue=0, speed=1_000_000):
    return {"username": username, "files": files, "hasFreeUploadSlot": free, "queueLength": queue, "uploadSpeed": speed}


def test_file_kind():
    assert file_kind(r"a\b\01 Track.FLAC") == "audio"
    assert file_kind(r"a\b\Album.log") == "log"
    assert file_kind(r"a\b\Album.cue") == "cue"
    assert file_kind(r"a\b\folder.jpg") == "image"
    assert file_kind(r"a\b\info.nfo") == "text"
    assert file_kind(r"a\b\list.m3u8") == "playlist"
    assert file_kind(r"a\b\README") == "other"


def test_disc_number():
    assert disc_number("CD1") == 1
    assert disc_number("CD 2") == 2
    assert disc_number("Disc 2 - Live") == 2
    assert disc_number("disk03") == 3
    assert disc_number("Side B") == 2
    assert disc_number("CD-ROM extras") is None
    assert disc_number("CDs") is None
    assert disc_number("Discography") is None


def test_place_disc_and_scans():
    assert place(r"Music\Artist\Album\CD1\01.flac", "audio").folder == ["Music", "Artist", "Album"]
    assert place(r"Music\Artist\Album\CD1\01.flac", "audio").disc == "CD1"
    p = place(r"Music\Artist\Album\Scans\front.jpg", "image")
    assert p.folder == ["Music", "Artist", "Album"] and p.in_scans
    p = place(r"Music\Artist\Album\Scans\Covers\front.jpg", "image")
    assert p.folder == ["Music", "Artist", "Album"] and p.in_scans
    p = place(r"Music\Artist\Album\CD2\Scans\disc.jpg", "image")
    assert p.folder == ["Music", "Artist", "Album"] and p.disc == "CD2"
    # A share root that is itself a disc folder is not merged into nothing.
    assert place(r"CD1\01.flac", "audio").folder == ["CD1"]


def test_groups_single_folder_with_sidecars():
    files = [f(rf"Music\Radiohead\1997 - OK Computer [CDP 7243 8 55229 2 5]\{n:02d} Track.flac", bitDepth=16, sampleRate=44100, length=250) for n in range(1, 13)]
    files += [
        f(r"Music\Radiohead\1997 - OK Computer [CDP 7243 8 55229 2 5]\OK Computer.log", 5_000),
        f(r"Music\Radiohead\1997 - OK Computer [CDP 7243 8 55229 2 5]\OK Computer.cue", 2_000),
        f(r"Music\Radiohead\1997 - OK Computer [CDP 7243 8 55229 2 5]\folder.jpg", 500_000),
        f(r"Music\Radiohead\1997 - OK Computer [CDP 7243 8 55229 2 5]\Scans\back.jpg", 4_000_000),
    ]
    drafts = candidate_drafts(group_responses([response("alice", files)], "radiohead ok computer"))
    assert len(drafts) == 1
    s = drafts[0].summary()
    assert s["audio_count"] == 12
    assert s["disc_counts"] == [12]
    assert s["audio_format"] == "FLAC"
    assert (s["bit_depth"], s["sample_rate"], s["mixed_quality"]) == (16, 44100, False)
    assert s["sidecars"] == {"log": 1, "cue": 1, "image": 2, "scans": 1}


def test_multi_disc_merges_and_orders_discs():
    root = r"share\Artist - Box (1995)"
    files = [f(rf"{root}\Disc 2 - Live\{n:02d}.flac") for n in range(1, 8)]
    files += [f(rf"{root}\CD1\{n:02d}.flac") for n in range(1, 11)]
    files += [f(rf"{root}\CD1\CD1.log", 4_000), f(rf"{root}\Disc 2 - Live\CD2.log", 4_000)]
    drafts = candidate_drafts(group_responses([response("bob", files)], "q"))
    assert len(drafts) == 1
    s = drafts[0].summary()
    assert drafts[0].folder == root
    assert s["disc_counts"] == [10, 7]
    assert s["sidecars"]["log"] == 2


def test_separate_users_and_folders_stay_separate():
    a = [f(rf"m\A\Album\{n}.flac") for n in range(3)]
    b = [f(rf"m\A\Album (2011 Remaster)\{n}.flac") for n in range(3)]
    drafts = group_responses([response("u1", a + b), response("u2", a)], "q")
    assert set(drafts) == {("u1", r"m\A\Album"), ("u1", r"m\A\Album (2011 Remaster)"), ("u2", r"m\A\Album")}


def test_image_only_folder_is_dropped():
    files = [f(r"m\A\Album\Artwork\front.jpg")]  # 'Artwork' attaches to m\A\Album, which has no audio
    files += [f(r"m\Pictures\cat.jpg")]
    assert candidate_drafts(group_responses([response("u", files)], "q")) == []


def test_fallback_query_merges_and_keeps_first_query():
    first = group_responses([response("u", [f(r"m\Album\01.flac")])], "primary")
    merged = group_responses([response("u", [f(r"m\Album\01.flac"), f(r"m\Album\02.flac")])], "fallback", first)
    draft = merged[("u", r"m\Album")]
    assert draft.query == "primary"
    assert len(draft.files) == 2


def test_mixed_format_and_quality():
    files = [
        f(r"m\Album\01.flac", bitDepth=24, sampleRate=96000),
        f(r"m\Album\02.flac", bitDepth=16, sampleRate=44100),
        f(r"m\Album\03.flac", bitDepth=24, sampleRate=96000),
        f(r"m\Album\04.mp3"),
    ]
    s = candidate_drafts(group_responses([response("u", files)], "q"))[0].summary()
    assert s["audio_format"] == "mixed"
    assert (s["bit_depth"], s["sample_rate"], s["mixed_quality"]) == (24, 96000, True)


def test_null_attributes_are_tolerated():
    files = [f(r"m\Album\01.flac", bitDepth=None, sampleRate=None, length=None)] * 3
    s = candidate_drafts(group_responses([response("u", files)], "q"))[0].summary()
    assert s["bit_depth"] is None and s["sample_rate"] is None and not s["mixed_quality"]


def test_default_filter():
    assert passes_default_filter("FLAC", 10, 10, flac_only=True)
    assert not passes_default_filter("MP3", 10, 10, flac_only=True)
    assert passes_default_filter("MP3", 10, 10, flac_only=False)
    assert not passes_default_filter("FLAC", 2, None, flac_only=True)
    assert not passes_default_filter("FLAC", 5, 12, flac_only=True)  # under half of 12
    assert passes_default_filter("FLAC", 6, 12, flac_only=True)


def test_availability_order():
    rows = [(False, 3, 9_000_000), (True, 0, 100_000), (True, 0, 2_000_000), (False, 1, 0)]
    assert sorted(rows, key=lambda r: availability_key(*r)) == [
        (True, 0, 2_000_000), (True, 0, 100_000), (False, 1, 0), (False, 3, 9_000_000),
    ]
