"""Transcode detection on synthetic audio, and (with ffmpeg) on a real MP3 round trip."""

import asyncio
import shutil
import subprocess
import wave

import numpy as np
import pytest

from picker.spectrum import Verdict, analyse, analyse_file, combine, file_verdict

RATE = 44100
rng = np.random.default_rng(7)


def noise(seconds=10, rate=RATE):
    return rng.standard_normal(int(seconds * rate)) * 0.1


def lowpass(x, cutoff, rate=RATE, floor_db=-95.0):
    """Brick-wall low-pass in the frequency domain, plus a dither-level noise floor."""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    spec[freqs > cutoff] = 0
    y = np.fft.irfft(spec, len(x))
    return y + rng.standard_normal(len(x)) * 10 ** (floor_db / 20)


def rolloff(x, start=8000, db_per_octave=12, rate=RATE):
    """A natural, gradual high-frequency roll-off (no cliff)."""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    gain_db = np.where(freqs > start, -db_per_octave * np.log2(np.maximum(freqs, 1) / start), 0)
    return np.fft.irfft(spec * 10 ** (gain_db / 20), len(x))


def test_full_band_noise_is_full():
    assert analyse(noise(), RATE).kind == "full"


@pytest.mark.parametrize("cutoff, hint", [
    (16000, "~128 kbps"),
    (19000, "~192-256 kbps"),
    (20000, "~256-320 kbps"),
])
def test_lowpassed_audio_is_lossy_with_its_cutoff(cutoff, hint):
    v = analyse(lowpass(noise(), cutoff), RATE)
    assert v.kind == "lossy"
    assert abs(v.cutoff_hz - cutoff) <= 300
    assert hint in v.detail


def test_gradual_rolloff_is_not_mistaken_for_a_cutoff():
    """Real recordings lose highs gradually; that is not a transcode."""
    assert analyse(rolloff(noise(), db_per_octave=18), RATE).kind == "full"


def test_upsampled_hires_is_flagged():
    rate = 96000
    v = analyse(lowpass(noise(rate=rate), 22050, rate), rate)
    assert v.kind == "upsampled" and "96 kHz" in v.detail


def test_genuine_hires_is_full():
    rate = 96000
    assert analyse(noise(rate=rate), rate).kind == "full"


def test_silence_is_unknown():
    assert analyse(np.zeros(RATE * 5), RATE).kind == "unknown"


def test_one_odd_window_does_not_condemn_a_file():
    full = Verdict("full", None, None, "ok")
    lossy = Verdict("lossy", 16000, 60.0, "cut")
    assert file_verdict([full, lossy, full]).kind == "full"
    assert file_verdict([lossy, lossy, full]).kind == "lossy"


def test_album_verdict_is_the_worst_file_and_the_lowest_cutoff():
    a = Verdict("lossy", 19000, 40.0, "a")
    b = Verdict("lossy", 16000, 60.0, "b")
    assert combine([Verdict("full", None, None, "x"), a, b]).cutoff_hz == 16000


# Real encoders (needs ffmpeg with libmp3lame; runs in CI and in the container) --------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def write_wav(path, samples, rate=RATE):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(stereo.tobytes())


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@needs_ffmpeg
def test_flac_from_a_128k_mp3_is_caught(tmp_path):
    write_wav(tmp_path / "src.wav", rolloff(noise(seconds=40), start=12000, db_per_octave=6))
    ffmpeg("-i", str(tmp_path / "src.wav"), "-c:a", "libmp3lame", "-b:a", "128k", str(tmp_path / "x.mp3"))
    ffmpeg("-i", str(tmp_path / "x.mp3"), "-c:a", "flac", str(tmp_path / "transcode.flac"))
    v = asyncio.run(analyse_file(str(tmp_path / "transcode.flac"), RATE, 40.0))
    assert v.kind == "lossy" and 15000 <= v.cutoff_hz <= 17500, v


@needs_ffmpeg
def test_flac_straight_from_pcm_is_full(tmp_path):
    write_wav(tmp_path / "src.wav", rolloff(noise(seconds=40), start=12000, db_per_octave=6))
    ffmpeg("-i", str(tmp_path / "src.wav"), "-c:a", "flac", str(tmp_path / "clean.flac"))
    assert asyncio.run(analyse_file(str(tmp_path / "clean.flac"), RATE, 40.0)).kind == "full"


def test_cd_anti_alias_rolloff_near_nyquist_is_full():
    """CD masters often drop steeply around 21-22 kHz (the ADC's anti-alias filter): not lossy."""
    assert analyse(lowpass(noise(), 21300), RATE).kind == "full"
    assert analyse(lowpass(noise(), 20500), RATE).kind == "lossy"  # LAME 320's low-pass is still caught
