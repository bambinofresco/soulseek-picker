"""Edition preferences (picker/preferences.py) and their effect on the default order."""

from picker import preferences
from picker.config import Settings
from picker.editions import Edition

GB = Edition(id="gb", title="OK Computer", date="1997-06-16", country="GB", formats=["CD"])
US = Edition(id="us", title="OK Computer", date="1997-07-01", country="US", formats=["CD"])
JP_SHM = Edition(id="jp", title="OK Computer", date="2009-03-25", country="JP", formats=["SHM-CD"], disambiguation="SHM-CD")
REMASTER = Edition(id="rm", title="OK Computer", date="2009-03-24", country="GB", formats=["CD"], disambiguation="remastered")
VINYL_XW = Edition(id="xw", title="OK Computer", date="2016-06-17", country="XW", formats=['12" Vinyl', '12" Vinyl'])
DELUXE = Edition(id="dx", title="OK Computer", date="2017-06-23", country="GB", formats=["CD", "CD", "CD"], disambiguation="OKNOTOK deluxe edition")


def settings(**kw) -> Settings:
    return Settings(config_dir="/tmp/x", download_dir="/tmp/d", music_dir="/tmp/m", slskd_api_key="k", **kw)


def test_edition_scores_and_reasons():
    s = settings()
    assert preferences.score_edition(GB, s).score == 4 + 3 and "+ GB" in preferences.score_edition(GB, s).reasons
    assert preferences.score_edition(US, s).score == 3 + 3
    assert preferences.score_edition(VINYL_XW, s).score == 4 + 2  # worldwide counts as the first country; vinyl is second
    rm = preferences.score_edition(REMASTER, s)
    assert rm.score == 4 + 3 - 10 and "− remaster" in rm.reasons
    assert preferences.score_edition(REMASTER, settings(avoid_remasters=False)).score == 7
    assert preferences.score_edition(JP_SHM, s).score == 1 + 3  # SHM-CD is a CD


def test_flags_and_depth_settings():
    s = settings(avoid_flags="Deluxe, Anniversary", prefer_flags="SHM-CD, Japan")
    assert preferences.score_edition(DELUXE, s).score == 4 + 3 - 3
    assert preferences.score_edition(JP_SHM, s).score == 1 + 3 + 3
    parsed = {"source": "CD", "flags": ["Deluxe", "Anniversary"], "territories": ["JP"]}
    c = preferences.score_candidate(parsed, 24, [], settings(preferred_depth="16", avoid_flags="Deluxe, Anniversary"))
    assert c.score == 3 + 1 - 2 - 3 - 3 and "− 24-bit" in c.reasons and "+ JP" in c.reasons
    assert preferences.score_candidate(parsed, 24, [], settings(preferred_depth="24")).score == 3 + 1 + 2
    assert preferences.score_candidate(parsed, None, [], settings(preferred_depth="24")).score == 3 + 1


def test_candidate_uses_the_best_of_its_possible_editions():
    s = settings()
    both = preferences.score_candidate({"source": "CD"}, 16, [GB, REMASTER], s)
    assert both.score == 4 + 3 - 3 and "− may be a remaster" in both.reasons
    only_remaster = preferences.score_candidate({"source": "CD"}, 16, [REMASTER], s)
    assert only_remaster.score == 4 + 3 - 10
    flagged = preferences.score_candidate({"source": "CD", "remaster": "folder"}, 16, [GB], s)
    assert flagged.score == 4 + 3 - 10
    no_editions = preferences.score_candidate({"remaster": "track"}, None, [], s)
    assert no_editions.score == 0 and no_editions.reasons == [] and no_editions.label == "0"


def test_best_edition_prefers_score_then_earliest():
    s = settings()
    assert preferences.best_edition([REMASTER, US, GB], s) is GB
    assert preferences.best_edition([REMASTER, JP_SHM], s) is JP_SHM
    assert preferences.best_edition([], s) is None
    assert preferences.best_edition([REMASTER, DELUXE], settings(avoid_remasters=False)) is REMASTER  # equal score: earlier


def test_empty_settings_score_nothing():
    s = settings(preferred_countries="", preferred_sources="", avoid_remasters=False)
    assert preferences.score_edition(REMASTER, s).score == 0
    assert preferences.score_candidate({"source": "CD", "remaster": "folder"}, 16, [GB], s).score == 0
