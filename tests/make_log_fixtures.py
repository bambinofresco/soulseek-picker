"""Regenerate tests/fixtures/logs/ from real MusicBrainz disc TOCs.

The TOCs (offsets and lead-out) come from MusicBrainz's own disc ID records for
OK Computer, so the expected IDs are MusicBrainz's, not this project's. Run:

    python tests/make_log_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import yaml

OUT = Path(__file__).parent / "fixtures" / "logs"

# MusicBrainz disc records (id, lead-out sectors, track offsets).
US_CD = ("rdosqrxASN.ZqF.d.5pPhHtgVyA-", 240687,
         [150, 21480, 50242, 70320, 90180, 112625, 132247, 141047, 158345, 179735, 196875, 216347])
EU_CD = ("Wrqc1L0b8KPTnhLcws8CnTgKWaw-", 241038,
         [150, 21538, 50328, 70517, 90398, 112841, 132450, 141254, 158546, 179931, 197052, 216548])
EU_CD_B = ("ae.KpaW2ZYdA4AZ5Xg_tclopOhY-", 240838,
           [150, 21480, 50243, 70320, 90180, 112625, 132248, 141048, 158345, 179735, 196875, 216348])


def rows(leadout: int, offsets: list[int], data_track_sectors: int = 0) -> list[tuple[int, int, int]]:
    """(track, start sector, end sector) as the rippers print them."""
    starts = [o - 150 for o in offsets]
    ends = [s - 1 for s in starts[1:]] + [leadout - 150 - 1]
    out = [(i + 1, s, e) for i, (s, e) in enumerate(zip(starts, ends))]
    if data_track_sectors:
        data_start = leadout - 150 + 11400
        out.append((len(out) + 1, data_start, data_start + data_track_sectors - 1))
    return out


def msf(sectors: int, xld: bool) -> str:
    m, rest = divmod(sectors, 75 * 60)
    s, f = divmod(rest, 75)
    return f"{m:02d}:{s:02d}:{f:02d}" if xld else f"{m}:{s:02d}.{f:02d}"


def toc_block(table: list[tuple[int, int, int]], xld: bool) -> str:
    lines = ["TOC of the extracted CD", "",
             "     Track |   Start  |  Length  | Start sector | End sector ",
             "    ---------------------------------------------------------"]
    for track, start, end in table:
        lines.append(f"       {track:2d}  | {msf(start, xld):>8} | {msf(end - start + 1, xld):>8} |    {start:6d}    |   {end:6d}   ")
    return "\n".join(lines) + "\n"


def eac(tables: list[list[tuple[int, int, int]]], *, errors: bool = False, secure: bool = True) -> str:
    parts = ["Exact Audio Copy V1.6 from 23. October 2020", "",
             "EAC extraction logfile from 21. October 2026, 12:00", "", "Radiohead / OK Computer", "",
             "Used drive  : PLEXTOR DVDR   PX-716A   Adapter: 1  ID: 0", "",
             f"Read mode               : {'Secure' if secure else 'Burst'}",
             "Utilize accurate stream : Yes", "Defeat audio cache      : Yes", ""]
    for table in tables:
        parts.append(toc_block(table, xld=False))
        for track, *_ in table:
            crc = f"{0x1A2B3C00 + track:08X}"
            copy = "DEADBEEF" if errors and track == 3 else crc
            parts += [f"Track {track:2d}", "", f"     Filename C:\\Rips\\{track:02d}.wav", "",
                      "     Peak level 98.8 %", "     Track quality 100.0 %",
                      f"     Test CRC {crc}", f"     Copy CRC {copy}",
                      "     Accurately ripped (confidence 12)  [ABCDEF12]  (AR v2)" if not errors else
                      "     Cannot be verified as accurate (confidence 3)  [ABCDEF12], AccurateRip returned [12345678]  (AR v2)",
                      "     Copy OK", ""]
    parts += ["There were errors" if errors else "No errors occurred", "", "End of status report", "",
              "==== Log checksum 0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF ===="]
    return "\r\n".join("\n".join(parts).split("\n")) + "\r\n"


def xld(tables: list[list[tuple[int, int, int]]]) -> str:
    parts = ["X Lossless Decoder version 20230627 (155.0)", "",
             "XLD extraction logfile from 2026-10-21 12:00:00 +0000", "", "Radiohead / OK Computer", "",
             "Used drive : HL-DT-ST BD-RE  WH16NS40 (revision 1.05)", "",
             "Ripper mode             : XLD Secure Ripper", "Disable audio cache     : OK", ""]
    for table in tables:
        parts.append(toc_block(table, xld=True))
        for track, *_ in table:
            crc = f"{0x2B3C4D00 + track:08X}"
            parts += [f"Track {track:02d}", f"    Filename : /Users/me/Rips/{track:02d}.flac",
                      f"    CRC32 hash (test run)  : {crc}", f"    CRC32 hash             : {crc}",
                      "    AccurateRip v1 signature : 1234ABCD",
                      "        ->Accurately ripped (v1+v2, confidence 3+2/5)", ""]
    parts += ["No errors occurred", "", "End of status report", "",
              "-----BEGIN XLD SIGNATURE-----", "AbCdEf0123456789", "-----END XLD SIGNATURE-----"]
    return "\n".join(parts) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    us, eu, eu_b = (rows(lo, offs) for _, lo, offs in (US_CD, EU_CD, EU_CD_B))
    enhanced = rows(EU_CD[1], EU_CD[2], data_track_sectors=30000)
    files = {
        "eac_utf16.log": (eac([us]).encode("utf-16"), [US_CD[0]]),  # BOM + UTF-16LE, CRLF
        "eac_errors.log": (eac([us], errors=True, secure=False).encode("utf-16"), [US_CD[0]]),
        "xld.log": (xld([eu]).encode("utf-8"), [EU_CD[0]]),
        "eac_enhanced_cd.log": (eac([enhanced]).encode("utf-16"), [EU_CD[0]]),
        "xld_two_disc.log": (xld([eu, eu_b]).encode("utf-8"), [EU_CD[0], EU_CD_B[0]]),
    }
    expected = {}
    for name, (data, ids) in files.items():
        (OUT / name).write_bytes(data)
        expected[name] = ids
    (OUT / "album.cue").write_text(
        'REM GENRE Alternative\nCATALOG 0724385522925\nPERFORMER "Radiohead"\nTITLE "OK Computer"\n'
        'FILE "01.wav" WAVE\n  TRACK 01 AUDIO\n    TITLE "Airbag"\n    ISRC GBAYE9700323\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    TITLE "Paranoid Android"\n    ISRC GBAYE9700324\n    INDEX 01 04:44:30\n',
        encoding="latin-1",
    )
    (OUT / "expected.yaml").write_text(yaml.safe_dump(expected, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
