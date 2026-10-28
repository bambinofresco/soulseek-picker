"""The uploader's tags against the chosen edition (picker/tagcheck.py)."""

import pytest
from mutagen.flac import FLAC

from picker.editions import Edition
from picker.tagcheck import TagCheck, check_folder, compare, read_tags
from tests.test_importing import make_flac

ORIGINAL = Edition(id="11111111-1111-1111-1111-111111111111", title="OK Computer", date="1997-06-16", country="GB",
                   labels=["Parlophone"], catalog_numbers=["NODATA 02"])
US_PRESSING = Edition(id="22222222-2222-2222-2222-222222222222", title="OK Computer", date="1997-07-01", country="US",
                      labels=["Capitol"], catalog_numbers=["CDP 7243 8 55229 2 5"])
REMASTER = Edition(id="33333333-3333-3333-3333-333333333333", title="OK Computer", date="2009-03-24", country="GB",
                   disambiguation="remastered, collector's edition")
DELUXE = Edition(id="44444444-4444-4444-4444-444444444444", title="OK Computer", date="1997-06-16", country="GB",
                 disambiguation="limited edition")
EDITIONS = [ORIGINAL, US_PRESSING, REMASTER, DELUXE]


def test_same_release_id():
    check = compare({"musicbrainz_albumid": ORIGINAL.id.upper()}, ORIGINAL, EDITIONS)
    assert check.kind == "same" and check.in_group and check.tagged_mbid == ORIGINAL.id


def test_sister_pressing_is_a_note_not_a_hold():
    check = compare({"musicbrainz_albumid": US_PRESSING.id, "date": "1997-07-01"}, ORIGINAL, EDITIONS)
    assert check.kind == "sibling" and check.tagged_mbid == US_PRESSING.id and check.in_group


@pytest.mark.parametrize("tagged, chosen, words", [
    (REMASTER, ORIGINAL, ["dated 2009, not 1997", "a remaster"]),
    (ORIGINAL, REMASTER, ["dated 1997, not 2009", "not a remaster"]),
    (DELUXE, ORIGINAL, ["'limited edition', not 'OK Computer'"]),
])
def test_another_edition_of_the_album_holds(tagged, chosen, words):
    check = compare({"musicbrainz_albumid": tagged.id}, chosen, EDITIONS)
    assert check.kind == "different" and check.in_group and check.tagged_mbid == tagged.id
    for word in words:
        assert word in check.detail, check.detail


def test_a_release_outside_the_album_holds_without_an_edition_to_offer():
    check = compare({"musicbrainz_albumid": "99999999-9999-9999-9999-999999999999"}, ORIGINAL, EDITIONS)
    assert check.kind == "different" and not check.in_group and "not an edition of this album" in check.detail


def test_garbage_release_id_is_ignored():
    assert compare({"musicbrainz_albumid": "not-a-uuid", "date": "1997"}, ORIGINAL, EDITIONS).kind == "agree"


@pytest.mark.parametrize("tags, kind, detail", [
    ({"album": "OK Computer (Remastered)"}, "different", "say remaster"),
    ({"comment": "2009 Remaster"}, "different", "say remaster"),
    ({"date": "2009"}, "different", "dated 2009; the chosen edition is from 1997"),
    ({"date": "1997-06-16"}, "agree", "year fits"),
    ({"album": "OK Computer"}, "untagged", "no release ID"),
    ({}, "untagged", "no release ID"),
])
def test_without_an_id_the_words_and_dates_decide(tags, kind, detail):
    check = compare(tags, ORIGINAL, EDITIONS)
    assert check.kind == kind and detail in check.detail, check


def test_remaster_words_are_fine_when_the_chosen_edition_is_one():
    assert compare({"album": "OK Computer (Remastered)", "date": "2009"}, REMASTER, EDITIONS).kind == "agree"


def test_no_chosen_edition():
    assert compare({"musicbrainz_albumid": REMASTER.id}, None, EDITIONS).kind == "untagged"


def test_read_tags_takes_the_most_common_value_and_reports_unreadable_files(tmp_path):
    files = []
    for n, mbid in enumerate([REMASTER.id, REMASTER.id, ORIGINAL.id], 1):
        path = tmp_path / f"{n:02d}.flac"
        make_flac(path, n)
        f = FLAC(path)
        f["MUSICBRAINZ_ALBUMID"] = mbid
        f["DATE"] = "2009"
        f.save()
        files.append(path)
    bad = tmp_path / "04.flac"
    bad.write_bytes(b"not a flac")
    tags, errors = read_tags(files + [bad])
    assert tags["musicbrainz_albumid"] == REMASTER.id and tags["date"] == "2009" and tags["album"] == "OK Computer"
    assert errors and errors[0].startswith("04.flac")

    check = check_folder(files, ORIGINAL, EDITIONS)
    assert check.kind == "different" and check.tagged_mbid == REMASTER.id
    assert check_folder([bad], ORIGINAL, EDITIONS).kind == "unreadable"
    assert TagCheck("same", "x").to_dict()["kind"] == "same"
