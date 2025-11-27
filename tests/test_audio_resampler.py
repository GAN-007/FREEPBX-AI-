import audioop
import pytest

from src.audio import (
    convert_pcm16le_to_target_format,
    mulaw_to_pcm16le,
    pcm16le_to_mulaw,
    resample_audio,
)


def test_mulaw_round_trip_identity():
    pcm_samples = audioop.tostereo(b"\x00\x10" * 40, 2, 1, 1)  # create dummy PCM16 data
    mono_pcm = audioop.tomono(pcm_samples, 2, 1, 0)
    mulaw = pcm16le_to_mulaw(mono_pcm)
    restored = mulaw_to_pcm16le(mulaw)
    assert len(restored) == len(mono_pcm)
    restored_rms = audioop.rms(restored, 2)
    original_rms = audioop.rms(mono_pcm, 2)
    assert restored_rms == pytest.approx(
        original_rms, abs=8
    )


def test_resample_identity_when_rates_match():
    pcm = b"\x01\x02" * 160
    converted, state = resample_audio(pcm, 8000, 8000)
    assert converted == pcm
    assert state == None


def test_convert_pcm_to_ulaw_format():
    pcm = b"\x01\x02" * 160
    ulaw = convert_pcm16le_to_target_format(pcm, "ulaw")
    assert len(ulaw) == len(pcm) // 2  # μ-law is 1 byte per sample