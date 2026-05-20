#!/usr/bin/env python3
"""
record_generator.py
3D Printable Vinyl Record Generator from Audio Files
音声ファイルから3Dプリンター用レコードSTL生成ツール

Usage / 使い方:
  python record_generator.py --help
  python record_generator.py --audio input.wav --size 12 --rpm 33 --groove mono --sides 1
"""

import argparse
import sys
import os
import math
import wave
import struct
import json
import time
from datetime import datetime

# ── Optional dependencies (graceful degradation) ──────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
# RECORD PHYSICAL SPECIFICATIONS / レコード物理仕様
# ──────────────────────────────────────────────────────────────────────────────

RECORD_SPECS = {
    # (outer_diameter_mm, inner_groove_radius_mm, label_radius_mm, thickness_mm)
    7:  {"outer_r": 87.0,  "groove_outer_r": 82.5, "groove_inner_r": 35.0, "label_r": 33.5,  "thickness": 1.8, "center_hole_r": 3.75},
    10: {"outer_r": 127.0, "groove_outer_r": 122.5,"groove_inner_r": 38.0, "label_r": 36.0,  "thickness": 1.8, "center_hole_r": 3.75},
    12: {"outer_r": 152.4, "groove_outer_r": 146.0,"groove_inner_r": 60.0, "label_r": 58.0,  "thickness": 2.0, "center_hole_r": 3.75},
}

RPM_GROOVE = {
    # groove_width_mm, groove_depth_mm, groove_spacing_mm (land width)
    33: {"width": 0.55, "depth": 0.30, "spacing": 0.30},
    45: {"width": 0.55, "depth": 0.30, "spacing": 0.28},
    78: {"width": 0.65, "depth": 0.35, "spacing": 0.25},
}

# ──────────────────────────────────────────────────────────────────────────────
# LANGUAGE / 言語
# ──────────────────────────────────────────────────────────────────────────────

LANG = {
    "en": {
        "title": "=== 3D Vinyl Record Generator ===",
        "loading": "Loading audio file...",
        "recording": "Recording from microphone...",
        "processing": "Processing audio...",
        "resampling": "Resampling to target rate...",
        "generating": "Generating groove path...",
        "building_stl": "Building STL geometry...",
        "writing_stl": "Writing STL file...",
        "done": "Done!",
        "error": "Error",
        "stats_header": "\n--- Record Specifications ---",
        "print_header": "\n--- Recommended Print Settings ---",
        "warn_long": "WARNING: Audio is longer than recommended max for this configuration.",
        "warn_pydub": "pydub not found. MP3 support disabled. Install with: pip install pydub",
        "warn_numpy": "numpy not found. Install with: pip install numpy",
        "max_duration": "Max recordable duration",
        "actual_duration": "Actual audio duration",
        "groove_turns": "Groove turns",
        "groove_length": "Total groove length",
        "stl_triangles": "STL triangles",
        "stl_file": "Output STL",
        "print_layer": "Layer height",
        "print_nozzle": "Nozzle diameter",
        "print_speed": "Print speed",
        "print_material": "Material",
        "print_infill": "Infill",
        "print_note": "Note",
        "print_note_val": "SLA/DLP resin strongly recommended for playable grooves",
        "sides_a": "Side A",
        "sides_b": "Side B",
    },
    "ja": {
        "title": "=== 3D ビニールレコード生成ツール ===",
        "loading": "音声ファイルを読み込んでいます...",
        "recording": "マイクから録音中...",
        "processing": "音声を処理中...",
        "resampling": "目標レートにリサンプリング中...",
        "generating": "溝パスを生成中...",
        "building_stl": "STLジオメトリを構築中...",
        "writing_stl": "STLファイルを書き込み中...",
        "done": "完了！",
        "error": "エラー",
        "stats_header": "\n--- レコード仕様 ---",
        "print_header": "\n--- 推奨印刷設定 ---",
        "warn_long": "警告: 音声がこの設定の推奨最大時間を超えています。",
        "warn_pydub": "pydubが見つかりません。MP3サポート無効。インストール: pip install pydub",
        "warn_numpy": "numpyが見つかりません。インストール: pip install numpy",
        "max_duration": "最大収録時間",
        "actual_duration": "実際の音声時間",
        "groove_turns": "溝の巻き数",
        "groove_length": "溝の総延長",
        "stl_triangles": "STL三角形数",
        "stl_file": "出力STLファイル",
        "print_layer": "積層ピッチ",
        "print_nozzle": "ノズル径",
        "print_speed": "印刷速度",
        "print_material": "材料",
        "print_infill": "充填率",
        "print_note": "注意",
        "print_note_val": "再生可能な溝にはSLA/DLP光造形が強く推奨されます",
        "sides_a": "A面",
        "sides_b": "B面",
    }
}

def t(key, lang="en"):
    return LANG.get(lang, LANG["en"]).get(key, key)

# ──────────────────────────────────────────────────────────────────────────────
# AUDIO LOADING
# ──────────────────────────────────────────────────────────────────────────────

def load_audio_wav(path):
    """Load WAV file, return (samples_float_array, sample_rate)"""
    with wave.open(path, 'rb') as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes   = wf.getnframes()
        raw = wf.readframes(nframes)

    if sampwidth == 1:
        fmt = f"{nframes * nchannels}B"
        data = struct.unpack(fmt, raw)
        samples = [((x - 128) / 128.0) for x in data]
    elif sampwidth == 2:
        fmt = f"{nframes * nchannels}h"
        data = struct.unpack(fmt, raw)
        samples = [x / 32768.0 for x in data]
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth}")

    # Mix to mono or split stereo
    if nchannels == 2:
        left  = samples[0::2]
        right = samples[1::2]
        return left, right, framerate
    else:
        return samples, samples, framerate

def load_audio_mp3(path):
    if not HAS_PYDUB:
        raise ImportError("pydub required for MP3. Install: pip install pydub")
    seg = AudioSegment.from_mp3(path)
    seg = seg.set_frame_rate(44100)
    left_ch  = seg.split_to_mono()[0]
    right_ch = seg.split_to_mono()[-1]  # same as left if mono
    to_float = lambda ch: [s / 32768.0 for s in ch.get_array_of_samples()]
    return to_float(left_ch), to_float(right_ch), seg.frame_rate

def load_audio(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        return load_audio_wav(path)
    elif ext == ".mp3":
        return load_audio_mp3(path)
    else:
        raise ValueError(f"Unsupported format: {ext}. Use WAV or MP3.")

# ──────────────────────────────────────────────────────────────────────────────
# AUDIO RESAMPLING (simple linear, no numpy required)
# ──────────────────────────────────────────────────────────────────────────────

def resample_simple(samples, orig_rate, target_rate):
    if orig_rate == target_rate:
        return samples
    ratio = orig_rate / target_rate
    new_len = int(len(samples) / ratio)
    out = []
    for i in range(new_len):
        src = i * ratio
        i0 = int(src)
        i1 = min(i0 + 1, len(samples) - 1)
        frac = src - i0
        out.append(samples[i0] * (1 - frac) + samples[i1] * frac)
    return out

def resample(samples, orig_rate, target_rate):
    if HAS_NUMPY:
        import numpy as np
        if orig_rate == target_rate:
            return list(samples)
        length = int(len(samples) * target_rate / orig_rate)
        return list(np.interp(
            np.linspace(0, len(samples) - 1, length),
            np.arange(len(samples)),
            samples
        ))
    return resample_simple(samples, orig_rate, target_rate)

# ──────────────────────────────────────────────────────────────────────────────
# GROOVE PATH CALCULATION
# ──────────────────────────────────────────────────────────────────────────────

def calc_max_duration(size_in, rpm):
    spec  = RECORD_SPECS[size_in]
    groove = RPM_GROOVE[rpm]
    r_out = spec["groove_outer_r"]
    r_in  = spec["groove_inner_r"]
    pitch = groove["width"] + groove["spacing"]   # mm per revolution radially
    turns = (r_out - r_in) / pitch
    # Arc length of spiral (approx)
    # circumference at mean radius * turns
    r_mean = (r_out + r_in) / 2.0
    groove_len_mm = 2 * math.pi * r_mean * turns
    # Linear velocity of groove under needle (IEC standard ~approx)
    # v = 2*pi*r*rpm/60  (mm/s) at mean radius
    v_mm_s = 2 * math.pi * r_mean * rpm / 60.0
    duration_s = groove_len_mm / v_mm_s
    return duration_s, turns, groove_len_mm

def build_groove_spiral(samples_left, samples_right, size_in, rpm, groove_mode, target_sr):
    """
    Returns list of (x, y, z_left, z_right) groove center points.
    z modulation = groove depth offset from baseline.
    """
    spec   = RECORD_SPECS[size_in]
    groove = RPM_GROOVE[rpm]

    r_out  = spec["groove_outer_r"]
    r_in   = spec["groove_inner_r"]
    pitch  = groove["width"] + groove["spacing"]
    depth  = groove["depth"]

    turns  = (r_out - r_in) / pitch
    total_angle = turns * 2 * math.pi  # total radians

    # samples per revolution at current sample rate
    # one revolution takes 60/rpm seconds
    secs_per_rev = 60.0 / rpm
    samples_per_rev = int(secs_per_rev * target_sr)

    total_samples = min(len(samples_left), len(samples_right))
    total_revs = total_samples / samples_per_rev
    total_revs = min(total_revs, turns)  # clamp to disc size

    # Number of groove points (one per sample, downsampled for STL size)
    # We use 360 points per revolution for reasonable STL size
    pts_per_rev = 360
    total_pts = int(total_revs * pts_per_rev)
    total_pts = max(total_pts, 720)

    path = []
    for i in range(total_pts):
        frac  = i / total_pts          # 0..1 progress
        angle = frac * total_revs * 2 * math.pi
        r     = r_out - frac * total_revs * pitch

        # Sample index
        si = int(frac * total_revs * samples_per_rev)
        si = min(si, total_samples - 1)

        sl = samples_left[si]
        sr = samples_right[si]

        x = r * math.cos(-angle)
        y = r * math.sin(-angle)

        if groove_mode == "mono":
            # Vertical (hill-and-dale) modulation
            z_mod = (sl + sr) / 2.0 * depth * 0.5
        else:
            # Stereo: left = z modulation, right = lateral (r modulation)
            z_mod = sl * depth * 0.4
            # lateral modulation baked into r slightly
            r += sr * groove["width"] * 0.15

        path.append((x, y, z_mod, r))

    return path, total_revs * 60.0 / rpm  # return duration in seconds

# ──────────────────────────────────────────────────────────────────────────────
# STL GENERATION
# ──────────────────────────────────────────────────────────────────────────────

def vec3_sub(a, b):  return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def vec3_cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def vec3_norm(v):
    l = math.sqrt(v[0]**2+v[1]**2+v[2]**2)
    if l == 0: return (0,0,1)
    return (v[0]/l, v[1]/l, v[2]/l)

def write_stl_binary(triangles, path):
    """Write binary STL. triangles = list of (n, v0, v1, v2) each a (x,y,z) tuple."""
    with open(path, 'wb') as f:
        f.write(b'\x00' * 80)  # header
        f.write(struct.pack('<I', len(triangles)))
        for (n, v0, v1, v2) in triangles:
            f.write(struct.pack('<fff', *n))
            f.write(struct.pack('<fff', *v0))
            f.write(struct.pack('<fff', *v1))
            f.write(struct.pack('<fff', *v2))
            f.write(struct.pack('<H', 0))

def make_tri(v0, v1, v2):
    n = vec3_norm(vec3_cross(vec3_sub(v1, v0), vec3_sub(v2, v0)))
    return (n, v0, v1, v2)

def generate_stl(groove_path, size_in, rpm, groove_mode, output_path, lang="en"):
    spec   = RECORD_SPECS[size_in]
    groove = RPM_GROOVE[rpm]
    T      = spec["thickness"]
    depth  = groove["depth"]
    w      = groove["width"] / 2.0
    hole_r = spec["center_hole_r"]
    label_r = spec["label_r"]
    outer_r = spec["outer_r"]

    triangles = []

    # ── Disc base (flat ring top surface) ─────────────────────────────────────
    # Approximated as polygon ring: outer edge and inner (center hole) edge
    N_DISC = 180
    def disc_top_ring(r_outer, r_inner, z, segs):
        tris = []
        for i in range(segs):
            a0 = 2*math.pi * i / segs
            a1 = 2*math.pi * (i+1) / segs
            o0 = (r_outer*math.cos(a0), r_outer*math.sin(a0), z)
            o1 = (r_outer*math.cos(a1), r_outer*math.sin(a1), z)
            i0 = (r_inner*math.cos(a0), r_inner*math.sin(a0), z)
            i1 = (r_inner*math.cos(a1), r_inner*math.sin(a1), z)
            tris.append(make_tri(o0, i0, o1))
            tris.append(make_tri(i0, i1, o1))
        return tris

    # Bottom face
    triangles += disc_top_ring(outer_r, hole_r, 0.0, N_DISC)
    # Top face (groove area will be cut in by z modulation)
    triangles += disc_top_ring(outer_r, hole_r, T, N_DISC)

    # Outer edge wall
    for i in range(N_DISC):
        a0 = 2*math.pi * i / N_DISC
        a1 = 2*math.pi * (i+1) / N_DISC
        b0 = (outer_r*math.cos(a0), outer_r*math.sin(a0), 0)
        b1 = (outer_r*math.cos(a1), outer_r*math.sin(a1), 0)
        t0 = (outer_r*math.cos(a0), outer_r*math.sin(a0), T)
        t1 = (outer_r*math.cos(a1), outer_r*math.sin(a1), T)
        triangles.append(make_tri(b0, b1, t0))
        triangles.append(make_tri(b1, t1, t0))

    # Inner hole wall
    for i in range(N_DISC):
        a0 = 2*math.pi * i / N_DISC
        a1 = 2*math.pi * (i+1) / N_DISC
        b0 = (hole_r*math.cos(a0), hole_r*math.sin(a0), 0)
        b1 = (hole_r*math.cos(a1), hole_r*math.sin(a1), 0)
        t0 = (hole_r*math.cos(a0), hole_r*math.sin(a0), T)
        t1 = (hole_r*math.cos(a1), hole_r*math.sin(a1), T)
        triangles.append(make_tri(b0, t0, b1))
        triangles.append(make_tri(b1, t0, t1))

    # ── Groove geometry ────────────────────────────────────────────────────────
    # Each groove segment: a V-shaped trench cut into the top face at z=T
    z_top = T
    n_pts = len(groove_path)

    for i in range(n_pts - 1):
        x0, y0, zm0, r0 = groove_path[i]
        x1, y1, zm1, r1 = groove_path[i+1]

        # Direction vector
        dx = x1 - x0
        dy = y1 - y0
        dl = math.sqrt(dx*dx + dy*dy)
        if dl < 1e-9:
            continue
        nx = -dy / dl  # normal to groove direction (lateral)
        ny =  dx / dl

        # Groove floor point (bottom of V)
        gz0 = z_top - depth + zm0 * 0.5
        gz1 = z_top - depth + zm1 * 0.5
        gz0 = max(0.1, min(gz0, z_top))
        gz1 = max(0.1, min(gz1, z_top))

        # Left wall top
        lx0, ly0 = x0 - nx * w, y0 - ny * w
        lx1, ly1 = x1 - nx * w, y1 - ny * w
        # Right wall top
        rx0, ry0 = x0 + nx * w, y0 + ny * w
        rx1, ry1 = x1 + nx * w, y1 + ny * w

        # Left wall of groove (V shape)
        v_floor0 = (x0, y0, gz0)
        v_floor1 = (x1, y1, gz1)
        v_left0  = (lx0, ly0, z_top)
        v_left1  = (lx1, ly1, z_top)
        v_right0 = (rx0, ry0, z_top)
        v_right1 = (rx1, ry1, z_top)

        triangles.append(make_tri(v_left0,  v_floor0, v_left1))
        triangles.append(make_tri(v_floor0, v_floor1, v_left1))
        triangles.append(make_tri(v_floor0, v_right0, v_floor1))
        triangles.append(make_tri(v_right0, v_right1, v_floor1))

    print(f"  {t('stl_triangles', lang)}: {len(triangles):,}")
    write_stl_binary(triangles, output_path)

# ──────────────────────────────────────────────────────────────────────────────
# REPORT GENERATION
# ──────────────────────────────────────────────────────────────────────────────

def print_report(size_in, rpm, groove_mode, sides, actual_dur, max_dur, turns,
                 groove_len, stl_files, lang="en"):
    print(t("stats_header", lang))
    spec = RECORD_SPECS[size_in]
    print(f"  Size           : {size_in}\" ({spec['outer_r']*2:.0f}mm diameter)")
    print(f"  RPM            : {rpm}")
    print(f"  Groove mode    : {groove_mode}")
    print(f"  Sides          : {sides}")
    print(f"  {t('max_duration', lang)}: {max_dur:.1f}s ({max_dur/60:.1f} min)")
    print(f"  {t('actual_duration', lang)}: {actual_dur:.1f}s ({actual_dur/60:.1f} min)")
    print(f"  {t('groove_turns', lang)}: {turns:.1f}")
    print(f"  {t('groove_length', lang)}: {groove_len/1000:.2f} m")
    for sf in stl_files:
        print(f"  {t('stl_file', lang)}: {sf}")

    print(t("print_header", lang))
    if rpm == 33:
        layer = "0.05–0.10 mm"
    else:
        layer = "0.05 mm"
    groove_w = RPM_GROOVE[rpm]["width"]
    print(f"  {t('print_layer', lang)}: {layer}")
    print(f"  {t('print_nozzle', lang)}: ≤ {groove_w:.2f} mm (SLA/DLP recommended)")
    print(f"  {t('print_speed', lang)}: 20–30 mm/s")
    print(f"  {t('print_material', lang)}: PLA / PETG (FDM) or Standard resin (SLA)")
    print(f"  {t('print_infill', lang)}: 100% (solid)")
    print(f"  {t('print_note', lang)}: {t('print_note_val', lang)}")

def save_json_report(size_in, rpm, groove_mode, sides, actual_dur, max_dur,
                     turns, groove_len, stl_files, output_json):
    spec = RECORD_SPECS[size_in]
    g    = RPM_GROOVE[rpm]
    data = {
        "generated_at": datetime.now().isoformat(),
        "record": {
            "size_inch": size_in,
            "diameter_mm": spec["outer_r"] * 2,
            "thickness_mm": spec["thickness"],
            "rpm": rpm,
            "groove_mode": groove_mode,
            "sides": sides,
        },
        "groove": {
            "width_mm": g["width"],
            "depth_mm": g["depth"],
            "spacing_mm": g["spacing"],
            "turns": round(turns, 2),
            "total_length_m": round(groove_len / 1000, 3),
        },
        "audio": {
            "max_duration_s": round(max_dur, 2),
            "actual_duration_s": round(actual_dur, 2),
        },
        "stl_files": stl_files,
        "print_settings": {
            "layer_height_mm": 0.05,
            "nozzle_mm": round(g["width"], 2),
            "infill_percent": 100,
            "material": "PLA/PETG or Resin",
            "recommended_printer": "SLA/DLP",
        }
    }
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return output_json

# ──────────────────────────────────────────────────────────────────────────────
# MAIN CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate 3D-printable vinyl record STL from audio / 音声から3Dレコードを生成"
    )
    parser.add_argument("--audio",   type=str, required=True,
                        help="Input audio file (WAV or MP3) / 入力音声ファイル")
    parser.add_argument("--size",    type=int, choices=[7, 10, 12], default=12,
                        help="Record size in inches / レコードサイズ (7/10/12, default: 12)")
    parser.add_argument("--rpm",     type=int, choices=[33, 45, 78], default=33,
                        help="Rotation speed / 回転数 (33/45/78, default: 33)")
    parser.add_argument("--groove",  type=str, choices=["mono", "stereo"], default="mono",
                        help="Groove modulation mode / 溝方式 (mono/stereo, default: mono)")
    parser.add_argument("--sides",   type=int, choices=[1, 2], default=1,
                        help="Number of sides / 面数 (1/2, default: 1)")
    parser.add_argument("--output",  type=str, default=None,
                        help="Output STL path prefix / 出力STLパスのプレフィックス")
    parser.add_argument("--lang",    type=str, choices=["en", "ja"], default="en",
                        help="Language / 言語 (en/ja, default: en)")
    parser.add_argument("--sr",      type=int, default=11025,
                        help="Internal sample rate for groove encoding / 内部サンプルレート (default: 11025)")
    args = parser.parse_args()

    lang = args.lang
    print(t("title", lang))
    print()

    if not HAS_NUMPY:
        print(f"  [WARN] {t('warn_numpy', lang)}")

    # Determine output prefix
    base = args.output or os.path.splitext(args.audio)[0]

    # ── Load audio ─────────────────────────────────────────────────────────────
    print(t("loading", lang))
    left_raw, right_raw, sr = load_audio(args.audio)
    print(f"  Loaded {len(left_raw)/sr:.1f}s @ {sr} Hz")

    # ── Resample ───────────────────────────────────────────────────────────────
    target_sr = args.sr
    if sr != target_sr:
        print(t("resampling", lang))
        left_raw  = resample(left_raw,  sr, target_sr)
        right_raw = resample(right_raw, sr, target_sr)

    # ── Check duration ─────────────────────────────────────────────────────────
    max_dur, turns, groove_len = calc_max_duration(args.size, args.rpm)
    actual_dur = len(left_raw) / target_sr
    if actual_dur > max_dur:
        print(f"  [WARN] {t('warn_long', lang)}")
        print(f"         Max={max_dur:.0f}s, Audio={actual_dur:.0f}s")
        print(f"         Audio will be truncated to {max_dur:.0f}s")
        clip = int(max_dur * target_sr)
        left_raw  = left_raw[:clip]
        right_raw = right_raw[:clip]
        actual_dur = max_dur

    # ── Generate grooves and STL ───────────────────────────────────────────────
    stl_files = []
    sides_list = [1] if args.sides == 1 else [1, 2]

    for side in sides_list:
        side_label = t("sides_a", lang) if side == 1 else t("sides_b", lang)
        print(f"\n{t('generating', lang)} ({side_label})")

        # Side B: reverse audio (plays from inner to outer like a real B side)
        if side == 2:
            sl = list(reversed(left_raw))
            sr_ = list(reversed(right_raw))
        else:
            sl = left_raw
            sr_ = right_raw

        groove_path, enc_dur = build_groove_spiral(
            sl, sr_, args.size, args.rpm, args.groove, target_sr
        )
        print(f"  Groove points: {len(groove_path):,}")

        print(t("building_stl", lang))
        suffix = f"_sideA" if side == 1 else f"_sideB"
        stl_path = f"{base}_{args.size}in_{args.rpm}rpm_{args.groove}{suffix}.stl"
        print(t("writing_stl", lang))
        generate_stl(groove_path, args.size, args.rpm, args.groove, stl_path, lang)
        stl_files.append(stl_path)
        print(f"  {t('done', lang)} → {stl_path}")

    # ── JSON report ────────────────────────────────────────────────────────────
    json_path = f"{base}_{args.size}in_{args.rpm}rpm_{args.groove}_report.json"
    save_json_report(args.size, args.rpm, args.groove, args.sides,
                     actual_dur, max_dur, turns, groove_len, stl_files, json_path)

    # ── Print report ───────────────────────────────────────────────────────────
    print_report(args.size, args.rpm, args.groove, args.sides,
                 actual_dur, max_dur, turns, groove_len, stl_files, lang)
    print(f"\n  Report JSON: {json_path}")
    print(f"\n{t('done', lang)}")


if __name__ == "__main__":
    main()
