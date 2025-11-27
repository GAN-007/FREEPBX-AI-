"""
Audio resampling and format conversion helpers.

These utilities provide common conversions required when bridging between
provider audio formats (OpenAI Realtime PCM16 @ 24 kHz, etc.) and the
AudioSocket expectations (typically μ-law or PCM16 at 8 kHz).
"""

from __future__ import annotations

import audioop
from typing import Optional, Tuple

# Default sample width for PCM16 little-endian audio
_PCM_SAMPLE_WIDTH = 2


def mulaw_to_pcm16le(data: bytes) -> bytes:
    """
    Convert μ-law audio data (8-bit) to PCM16 little-endian samples.
    """
    if not data:
        return b""
    return audioop.ulaw2lin(data, _PCM_SAMPLE_WIDTH)


def pcm16le_to_mulaw(data: bytes) -> bytes:
    """
    Convert PCM16 little-endian samples to μ-law (8-bit) encoding.
    """
    if not data:
        return b""
    return audioop.lin2ulaw(data, _PCM_SAMPLE_WIDTH)


def resample_audio(
    pcm_bytes: bytes,
    source_rate: int,
    target_rate: int,
    *,
    sample_width: int = _PCM_SAMPLE_WIDTH,
    channels: int = 1,
    state: Optional[tuple] = None,
) -> Tuple[bytes, Optional[tuple]]:
    """
    Resample PCM audio between sample rates using audioop.ratecv.

    Returns a tuple of (converted_bytes, new_state) so callers can maintain
    continuity between sequential calls.
    """
    if not pcm_bytes or source_rate == target_rate:
        return pcm_bytes, state

    converted, new_state = audioop.ratecv(
        pcm_bytes, sample_width, channels, source_rate, target_rate, state
    )
    return converted, new_state


def convert_pcm16le_to_target_format(pcm_bytes: bytes, target_format: str) -> bytes:
    """
    Convert PCM16 little-endian audio into the target encoding.

    Currently supports μ-law and PCM16 (no-op for PCM targets).
    """
    if not pcm_bytes:
        return b""

    fmt = (target_format or "").lower()
    if fmt in ("ulaw", "mulaw", "mu-law"):
        return pcm16le_to_mulaw(pcm_bytes)
    # Default: assume PCM target
    return pcm_bytes