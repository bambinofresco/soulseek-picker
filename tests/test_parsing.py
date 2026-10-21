from pathlib import Path

import pytest
import yaml

from picker.parsing import normalize_catalog, parse_candidate

FIXTURES = yaml.safe_load((Path(__file__).parent / "fixtures" / "folder_names.yaml").read_text(encoding="utf-8"))

CHECKED = {
    "original_year": None, "reissue_year": None, "catalog_numbers": [], "barcodes": [], "label": None,
    "source": None, "store": None, "bit_depth": None, "sample_rate": None, "mixed_quality": False,
    "remaster": None, "flags": [], "territories": [], "rip_tool": None,
}
CHECKED_IF_LISTED = {"sidecars", "quality_from"}
UNORDERED = {"flags", "territories"}
DEFAULT_FILES = ["01 Track.flac", "02 Track.flac", "03 Track.flac"]


def _cases():
    for group in FIXTURES:
        for case in group["cases"]:
            yield pytest.param(group, case, id=case["path"])


def _inputs(case):
    paths, files = [], []
    for entry in case.get("files", DEFAULT_FILES):
        entry = {"name": entry} if isinstance(entry, str) else entry
        filename = case["path"] + "\\" + entry["name"]
        paths.append(filename)
        files.append({"filename": filename, "bitDepth": entry.get("bitDepth"), "sampleRate": entry.get("sampleRate")})
    return paths, files


def test_corpus_size():
    assert sum(len(g["cases"]) for g in FIXTURES) >= 80


@pytest.mark.parametrize("group, case", list(_cases()))
def test_fixture(group, case):
    paths, files = _inputs(case)
    parsed = parse_candidate(
        paths, files=files, artist=group["artist"], album=str(group["album"]),
        labels=group.get("labels", []), current_year=2026,
    ).to_dict()
    expect = case.get("expect") or {}
    unknown = set(expect) - set(CHECKED) - CHECKED_IF_LISTED
    assert not unknown, f"unknown expect keys {unknown}"

    for name, empty in CHECKED.items():
        want = expect.get(name, empty)
        got = parsed[name]
        if name in UNORDERED:
            want, got = sorted(want), sorted(got)
        assert got == want, f"{name}: got {got!r}, want {want!r}\nevidence: {parsed['evidence'].get(name)}"
    for name in CHECKED_IF_LISTED & set(expect):
        assert parsed[name] == expect[name], name


def test_every_value_has_evidence():
    group = FIXTURES[0]
    paths, files = _inputs({"path": r"Music\Radiohead\OK Computer (1997) [2009 Remaster] [Japan TOCP-50201] [WEB 24-96] [Parlophone]"})
    parsed = parse_candidate(paths, files=files, artist=group["artist"], album=group["album"], labels=group["labels"])
    for key in ("years", "catalog_numbers", "territories", "source", "quality", "remaster", "label"):
        assert parsed.evidence.get(key), key
    ev = parsed.evidence["catalog_numbers"][0]
    assert ev.token == "TOCP-50201"
    assert ev.segment.startswith("OK Computer (1997)")


def test_normalize_catalog():
    assert normalize_catalog("cdp 7243-8.55229") == "CDP7243855229"


def test_empty_input():
    parsed = parse_candidate([])
    assert parsed.to_dict()["catalog_numbers"] == [] and parsed.source is None
