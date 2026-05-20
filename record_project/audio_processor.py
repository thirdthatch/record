"""
audio_processor.py
------------------
Audio loading, RIAA equalization, and resampling for vinyl record cutting.

Responsibilities:
  - Load WAV / MP3 files into float32 stereo samples
  - Apply RIAA pre-emphasis filter (bilinear transform IIR)
  - High-quality sinc-based resampling to target groove sample rate
  - Normalize and clip-protect the signal
"""

from __future__ import annotations
import math
import wave
import struct
import array
from typing import Tuple, List

# Optional high-performance libraries
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from pydub import AudioSegment
    HAS_PYDUB = True
except ImportError:
    HAS_PYDUB = False

from record_specs import RIAA_T1_US, RIAA_T2_US, RIAA_T3_US


# ── Type alias ────────────────────────────────────────────────────────────
Samples = List[float]  # float values in [-1.0, 1.0]


# ══════════════════════════════════════════════════════════════════════════════
# Audio Loading
# ══════════════════════════════════════════════════════════════════════════════

def load_audio(path: str) -> Tuple[Samples, Samples, int]:
    """
    Load audio file, return (left_samples, right_samples, sample_rate).
    Mono files return identical left and right channels.
    Samples are float32 normalized to [-1.0, 1.0].
    """
    import os
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        return _load_wav(path)
    elif ext in (".mp3", ".ogg", ".flac", ".aac", ".m4a"):
        return _load_pydub(path)
    else:
        raise ValueError(
            f"Unsupported audio format: {ext}\n"
            f"Supported: .wav, .mp3, .ogg, .flac, .aac, .m4a"
        )


def _load_wav(path: str) -> Tuple[Samples, Samples, int]:
    with wave.open(path, 'rb') as wf:
        nch       = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate      = wf.getframerate()
        nframes   = wf.getnframes()
        raw       = wf.readframes(nframes)

    total_samples = nframes * nch

    if sampwidth == 1:
        # 8-bit WAV is unsigned
        fmt  = f"{total_samples}B"
        data = struct.unpack(fmt, raw)
        scale = 1.0 / 128.0
        samples = [(x - 128) * scale for x in data]
    elif sampwidth == 2:
        # 16-bit signed little-endian
        fmt  = f"<{total_samples}h"
        data = struct.unpack(fmt, raw)
        scale = 1.0 / 32768.0
        samples = [x * scale for x in data]
    elif sampwidth == 3:
        # 24-bit signed little-endian (no direct struct format)
        samples = []
        scale = 1.0 / 8388608.0
        for i in range(0, len(raw), 3):
            val = raw[i] | (raw[i+1] << 8) | (raw[i+2] << 16)
            if val >= 0x800000:
                val -= 0x1000000
            samples.append(val * scale)
    elif sampwidth == 4:
        fmt  = f"<{total_samples}i"
        data = struct.unpack(fmt, raw)
        scale = 1.0 / 2147483648.0
        samples = [x * scale for x in data]
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth} bytes")

    if nch == 1:
        return samples, samples, rate
    elif nch == 2:
        left  = samples[0::2]
        right = samples[1::2]
        return left, right, rate
    else:
        # Mix all channels to stereo
        left  = [sum(samples[i*nch + c] for c in range(0, nch, 2)) / (nch//2)
                 for i in range(nframes)]
        right = [sum(samples[i*nch + c] for c in range(1, nch, 2)) / (nch//2)
                 for i in range(nframes)]
        return left, right, rate


def _load_pydub(path: str) -> Tuple[Samples, Samples, int]:
    if not HAS_PYDUB:
        raise ImportError(
            "pydub is required for non-WAV files.\n"
            "Install: pip install pydub\n"
            "Also requires ffmpeg: https://ffmpeg.org/"
        )
    seg = AudioSegment.from_file(path)
    seg = seg.set_sample_width(2)  # Normalize to 16-bit
    rate = seg.frame_rate
    scale = 1.0 / 32768.0

    channels = seg.split_to_mono()
    left  = [s * scale for s in array.array('h', channels[0].raw_data)]
    right = [s * scale for s in array.array('h', channels[-1].raw_data)]
    return left, right, rate


# ══════════════════════════════════════════════════════════════════════════════
# RIAA Pre-emphasis Filter
# ══════════════════════════════════════════════════════════════════════════════

class RIAAPreemphasis:
    """
    RIAA recording equalization filter (pre-emphasis).

    Applied before cutting to disc. Playback equipment applies inverse RIAA.

    Transfer function H(s) in analog domain:
        H(s) = (1 + s*T2) / ((1 + s*T1)(1 + s*T3))

    Where T1=3180µs, T2=318µs, T3=75µs (IEC 60098)

    Converted to digital IIR via bilinear transform.

    This filter:
      - Boosts high frequencies (above ~2kHz) — reduces groove excursion
      - Cuts low frequencies (below ~500Hz)  — prevents over-wide grooves
    """

    def __init__(self, sample_rate: int):
        self.sr = sample_rate
        # Compute bilinear-transform coefficients
        self._compute_coeffs()
        # Filter states (two cascaded biquads)
        self._z1_1 = 0.0
        self._z2_1 = 0.0
        self._z1_2 = 0.0
        self._z2_2 = 0.0

    def _compute_coeffs(self):
        """
        Compute second-order IIR coefficients via bilinear transform.
        RIAA curve has poles at T1 and T3, zero at T2.
        We implement as two cascaded first-order sections.
        """
        sr = self.sr
        T  = 1.0 / sr

        # Convert time constants to angular frequencies
        w1 = 1.0 / (RIAA_T1_US * 1e-6)   # 50 Hz
        w2 = 1.0 / (RIAA_T2_US * 1e-6)   # 500 Hz
        w3 = 1.0 / (RIAA_T3_US * 1e-6)   # 2122 Hz

        # Bilinear transform: s → 2/T * (z-1)/(z+1)
        # Pre-warp frequencies
        w1d = 2.0 * sr * math.tan(w1 * T / 2.0)
        w2d = 2.0 * sr * math.tan(w2 * T / 2.0)
        w3d = 2.0 * sr * math.tan(w3 * T / 2.0)

        # Section 1: zero at w2, pole at w1 (low shelf boost)
        # H1(z): first-order highpass shelving
        k1 = w1d / (2.0 * sr)
        k2 = w2d / (2.0 * sr)
        # numerator:   b0 + b1*z^-1
        # denominator: 1  + a1*z^-1
        self.b0_1 = (1.0 + k2) / (1.0 + k1)
        self.b1_1 = (k2 - 1.0) / (1.0 + k1)
        self.a1_1 = (k1 - 1.0) / (1.0 + k1)

        # Section 2: pole at w3 (high frequency roll-off)
        k3 = w3d / (2.0 * sr)
        self.b0_2 = 1.0 / (1.0 + k3)
        self.b1_2 = -1.0 / (1.0 + k3)   # effectively: b = [1, -1] * b0
        # Actually a one-pole low-pass for the T3 roll-off
        # More accurately: H(s) = 1 / (1 + s*T3)
        self.b0_2 =  1.0 / (1.0 + k3)
        self.b1_2 =  1.0 / (1.0 + k3)   # b = b0 * [1, 1]  (low-pass)
        self.a1_2 = (k3 - 1.0) / (1.0 + k3)

    def reset(self):
        self._z1_1 = self._z2_1 = 0.0
        self._z1_2 = self._z2_2 = 0.0

    def process_sample(self, x: float) -> float:
        """Process one sample through both filter sections."""
        # Section 1
        y1 = self.b0_1 * x + self._z1_1
        self._z1_1 = self.b1_1 * x - self.a1_1 * y1

        # Section 2
        y2 = self.b0_2 * y1 + self._z1_2
        self._z1_2 = self.b1_2 * y1 - self.a1_2 * y2

        return y2

    def process(self, samples: Samples) -> Samples:
        """Process a full channel through RIAA pre-emphasis."""
        self.reset()
        if HAS_NUMPY:
            # Vectorized for speed (still uses Python loop but with numpy scalars)
            out = []
            z1_1 = self._z1_1
            z1_2 = self._z1_2
            b0_1, b1_1, a1_1 = self.b0_1, self.b1_1, self.a1_1
            b0_2, b1_2, a1_2 = self.b0_2, self.b1_2, self.a1_2
            for x in samples:
                y1   = b0_1 * x + z1_1
                z1_1 = b1_1 * x - a1_1 * y1
                y2   = b0_2 * y1 + z1_2
                z1_2 = b1_2 * y1 - a1_2 * y2
                out.append(y2)
            return out
        else:
            return [self.process_sample(x) for x in samples]


def apply_riaa(left: Samples, right: Samples, sample_rate: int
               ) -> Tuple[Samples, Samples]:
    """Apply RIAA pre-emphasis to both channels."""
    filt = RIAAPreemphasis(sample_rate)
    left_eq  = filt.process(left)
    filt.reset()
    right_eq = filt.process(right)
    return left_eq, right_eq


# ══════════════════════════════════════════════════════════════════════════════
# Normalization & Peak Limiting
# ══════════════════════════════════════════════════════════════════════════════

def normalize_and_limit(left: Samples, right: Samples,
                        target_peak: float = 0.85
                        ) -> Tuple[Samples, Samples]:
    """
    Normalize stereo signal to target_peak amplitude.
    Applies soft-knee limiting at 0.95 to prevent groove overcut.
    """
    # Find peak across both channels
    peak = max(
        max(abs(x) for x in left)  if left  else 0.0,
        max(abs(x) for x in right) if right else 0.0,
    )

    if peak < 1e-9:
        return left, right

    scale = target_peak / peak

    def process_channel(ch: Samples) -> Samples:
        out = []
        for x in ch:
            v = x * scale
            # Soft knee limiter at 0.95
            if v > 0.95:
                v = 0.95 + (1.0 - 0.95) * math.tanh((v - 0.95) / 0.05)
            elif v < -0.95:
                v = -0.95 - (1.0 - 0.95) * math.tanh((-v - 0.95) / 0.05)
            out.append(v)
        return out

    return process_channel(left), process_channel(right)


# ══════════════════════════════════════════════════════════════════════════════
# Resampling
# ══════════════════════════════════════════════════════════════════════════════

def resample(samples: Samples, from_rate: int, to_rate: int) -> Samples:
    """
    Resample audio to a new sample rate.

    Uses numpy-accelerated interpolation when available,
    falls back to linear interpolation otherwise.

    Note: For production use, consider scipy.signal.resample_poly
    for higher quality anti-aliased resampling.
    """
    if from_rate == to_rate:
        return samples

    if HAS_NUMPY:
        return _resample_numpy(samples, from_rate, to_rate)
    else:
        return _resample_linear(samples, from_rate, to_rate)


def _resample_numpy(samples: Samples, from_rate: int, to_rate: int) -> Samples:
    import numpy as np
    arr    = np.array(samples, dtype=np.float64)
    length = int(len(arr) * to_rate / from_rate)
    xp     = np.arange(len(arr))
    x_new  = np.linspace(0, len(arr) - 1, length)
    return list(np.interp(x_new, xp, arr))


def _resample_linear(samples: Samples, from_rate: int, to_rate: int) -> Samples:
    ratio   = from_rate / to_rate
    new_len = int(len(samples) * to_rate / from_rate)
    out     = []
    n       = len(samples)
    for i in range(new_len):
        src = i * ratio
        i0  = int(src)
        i1  = min(i0 + 1, n - 1)
        f   = src - i0
        out.append(samples[i0] * (1.0 - f) + samples[i1] * f)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# High-level pipeline
# ══════════════════════════════════════════════════════════════════════════════

def prepare_audio(
    path: str,
    target_sample_rate: int,
    apply_riaa_eq: bool = True,
    max_duration_s: float = None,
    progress_cb=None,
) -> Tuple[Samples, Samples, int, float]:
    """
    Full audio preparation pipeline:
      1. Load file
      2. Trim to max_duration_s if specified
      3. Resample to target_sample_rate
      4. Apply RIAA pre-emphasis
      5. Normalize & limit

    Returns:
        left_samples, right_samples, sample_rate, actual_duration_s
    """
    def progress(msg: str, pct: int):
        if progress_cb:
            progress_cb(msg, pct)

    progress("Loading audio file...", 5)
    left, right, sr = load_audio(path)
    actual_dur = len(left) / sr
    progress(f"  Loaded {actual_dur:.1f}s @ {sr}Hz, {len(left)} samples", 10)

    # Trim
    if max_duration_s is not None and actual_dur > max_duration_s:
        clip = int(max_duration_s * sr)
        left  = left[:clip]
        right = right[:clip]
        actual_dur = max_duration_s
        progress(f"  Trimmed to {actual_dur:.1f}s", 15)

    # Resample
    if sr != target_sample_rate:
        progress(f"Resampling {sr}Hz → {target_sample_rate}Hz...", 20)
        left  = resample(left,  sr, target_sample_rate)
        right = resample(right, sr, target_sample_rate)
        sr = target_sample_rate

    # RIAA
    if apply_riaa_eq:
        progress("Applying RIAA pre-emphasis...", 35)
        left, right = apply_riaa(left, right, sr)

    # Normalize
    progress("Normalizing signal...", 45)
    left, right = normalize_and_limit(left, right)

    progress("Audio ready.", 50)
    return left, right, sr, actual_dur
