import pytest

from picker.queries import fallback_queries, fold, normalize, primary_query


@pytest.mark.parametrize("text, expected", [
    ("Björk", "bjork"),
    ("Sigur Rós", "sigur ros"),
    ("Mötley Crüe", "motley crue"),
    ("Øystein Sevåg", "oystein sevag"),
    ("The Beatles", "beatles"),
    ("Theatre of Tragedy", "theatre of tragedy"),
    ("Abbey Road (Remastered)", "abbey road"),
    ("Nevermind [Deluxe Edition]", "nevermind"),
    ("Don't Stand So Close", "dont stand so close"),
    ("Simon & Garfunkel", "simon garfunkel"),
    ("AC/DC", "ac dc"),
    ("…And Justice for All", "and justice for all"),
    ("The", "the"),
])
def test_normalize(text, expected):
    assert normalize(text) == expected


def test_fold_keeps_plain_ascii():
    assert fold("Radiohead") == "Radiohead"


def test_primary_query():
    assert primary_query("The Beatles", "Abbey Road (2019 Mix)") == "beatles abbey road"
    assert primary_query("Björk", "Homogenic") == "bjork homogenic"


def test_fallbacks_album_alone_needs_two_distinctive_words():
    assert fallback_queries("Radiohead", "OK Computer") == ["radiohead computer"]
    assert fallback_queries("Pink Floyd", "The Dark Side of the Moon") == [
        "dark side of the moon",
        "pink floyd dark",
    ]


def test_fallbacks_skip_duplicates_of_primary():
    # Single-word album: artist + strong word is the primary query itself.
    assert fallback_queries("Björk", "Homogenic") == []


def test_fallbacks_without_distinctive_words():
    assert fallback_queries("Weezer", "The Blue Album") == ["blue album", "weezer blue"]
    assert fallback_queries("X", "IV") == []
