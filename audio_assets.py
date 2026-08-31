import math
import wave
from pathlib import Path

import pjsua2 as pj

from config import (
    CALLS_OUTPUT_DIR,
    INPUT_AUDIO_DIR,
    TX_CHANNEL_COUNT,
    TX_CLOCK_RATE,
    TX_FRAME_BYTES,
    TX_SAMPLE_WIDTH_BYTES,
)
from run_logging import log_info


try:
    import audioop as _audioop

    def frame_rms(pcm: bytes) -> float:
        return float(_audioop.rms(pcm, 2))

except ImportError:  # audioop was removed in Python 3.13
    _audioop = None
    import array

    def frame_rms(pcm: bytes) -> float:
        usable = len(pcm) // 2 * 2
        if usable <= 0:
            return 0.0
        samples = array.array("h")
        samples.frombytes(pcm[:usable])
        subset = samples[::4]
        if not subset:
            return 0.0
        return math.sqrt(sum(sample * sample for sample in subset) / len(subset))


def build_recording_path(kind: str, call_id: int) -> str:
    out_dir = Path(CALLS_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"{kind}_{call_id:02d}.wav")


def resolve_input_audio(filename: str) -> str:
    path = Path(filename)
    if path.is_absolute():
        return str(path)
    return str(INPUT_AUDIO_DIR / path)


def load_wav_frames(filename: str):
    """Load one WAV and convert it to fixed 20 ms PCM frames."""
    path = Path(resolve_input_audio(filename))
    if not path.is_file():
        raise FileNotFoundError(f"required input audio is missing: {path}")

    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        source_rate = wav_file.getframerate()
        compression = wav_file.getcomptype()
        pcm = wav_file.readframes(wav_file.getnframes())

    if compression != "NONE":
        raise ValueError(f"{path}: compressed WAV is unsupported ({compression})")
    if channels != TX_CHANNEL_COUNT or sample_width != TX_SAMPLE_WIDTH_BYTES:
        raise ValueError(
            f"{path}: expected mono 16-bit PCM, got "
            f"channels={channels}, sample_width={sample_width}"
        )
    if not pcm:
        raise ValueError(f"{path}: WAV contains no audio frames")

    if source_rate != TX_CLOCK_RATE:
        if _audioop is None:
            raise ValueError(
                f"{path}: sample rate is {source_rate} Hz, but {TX_CLOCK_RATE} Hz "
                "is required and Python audioop is unavailable; convert the file first"
            )
        pcm, _ = _audioop.ratecv(
            pcm,
            TX_SAMPLE_WIDTH_BYTES,
            TX_CHANNEL_COUNT,
            source_rate,
            TX_CLOCK_RATE,
            None,
        )

    frames = []
    for offset in range(0, len(pcm), TX_FRAME_BYTES):
        chunk = pcm[offset : offset + TX_FRAME_BYTES]
        if len(chunk) < TX_FRAME_BYTES:
            chunk += b"\x00" * (TX_FRAME_BYTES - len(chunk))
        frames.append(pj.ByteVector(chunk))

    duration_ms = len(pcm) * 1000 / (
        TX_CLOCK_RATE * TX_CHANNEL_COUNT * TX_SAMPLE_WIDTH_BYTES
    )
    log_info(
        f"*** loaded {filename}: {source_rate} Hz -> {TX_CLOCK_RATE} Hz, "
        f"duration={duration_ms:.0f} ms, frames={len(frames)}"
    )
    return tuple(frames)


def load_audio_assets(actions):
    """Load each unique action WAV once before any call starts."""
    assets = {}
    for action_type, action_value in actions:
        if action_type == "wav" and action_value not in assets:
            assets[action_value] = load_wav_frames(action_value)
    return assets
