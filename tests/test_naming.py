import pytest

from picker.naming import FolderValues, album_folder, clean_value, unknown_tokens

LIDARR_DEFAULT = "{Album Title} ({Release Year})"


@pytest.mark.parametrize("title, year, expected", [
    ("OK Computer", "1997", "OK Computer (1997)"),
    ("Who Made Who: Live", "1986", "Who Made Who - Live (1986)"),  # Smart colon
    ("Ratio:Edge", "2001", "Ratio-Edge (2001)"),
    ("AC/DC Live", "1992", "AC+DC Live (1992)"),
    ("What's Going On?", "1971", "What's Going On! (1971)"),
    ('"Heroes"', "1977", "Heroes (1977)"),
    ("*", "2005", "- (2005)"),
    ("...Baby One More Time", "1999", "Baby One More Time (1999)"),  # leading dots trimmed
    ("Untitled", None, "Untitled (Unknown)"),  # Lidarr renders a missing year as Unknown
    ("Spaced  Out", "2000", "Spaced Out (2000)"),  # repeated separators collapse
])
def test_matches_lidarr_album_folder(title, year, expected):
    assert album_folder(LIDARR_DEFAULT, FolderValues(album_title=title, release_year=year)) == expected


def test_optional_catalog_number_block_disappears_when_empty():
    template = "{Album Title} ({Release Year}){ [Catalog Number]}"
    with_cat = FolderValues(album_title="OK Computer", release_year="1997", catalog_number="CDP 7243 8 55229 2 5")
    assert album_folder(template, with_cat) == "OK Computer (1997) [CDP 7243 8 55229 2 5]"
    without = FolderValues(album_title="OK Computer", release_year="1997")
    assert album_folder(template, without) == "OK Computer (1997)"


def test_extras_and_aliases():
    v = FolderValues(album_title="OK Computer", release_year="1997", artist_name="Radiohead", label="Parlophone",
                     country="GB", media="CD", edition_date="1997-06-16")
    assert album_folder("{Album Title} ({Edition Year}, {Country}, {Media}) {[Label]}", v) == \
        "OK Computer (1997, GB, CD) [Parlophone]"
    assert album_folder("{album} ({year})", v) == "OK Computer (1997)"
    assert album_folder("{Album.Title}", v) == "OK.Computer"  # separator inside the token, as in Lidarr


def test_reserved_names_and_trailing_separators():
    assert album_folder("{Album Title}", FolderValues(album_title="con.test")) == "con_test"
    assert album_folder("{Album Title} -", FolderValues(album_title="X")) == "X"


def test_unknown_tokens_are_rejected():
    assert unknown_tokens("{Album Title} {Bogus Field}") == ["Bogus Field"]
    with pytest.raises(ValueError, match="Bogus"):
        album_folder("{Bogus Field}", FolderValues(album_title="x"))


def test_clean_value():
    assert clean_value("  Title: Sub  ") == "Title - Sub"
    assert clean_value("a<b>c|d") == "abcd"
