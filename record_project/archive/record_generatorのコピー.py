#!/usr/bin/env python3
"""
record_generator.py
--------------------
3D-printable vinyl record generator — CLI entry point.

Rust-backed pipeline
─────────────
  1. Load or convert audio to WAV
  2. Pass audio and record parameters to Rust backend
  3. Rust resamples, applies RIAA, normalises, computes groove and writes STL
  4. Write JSON report from Rust output

Usage
─────
  python record_generator.py --audio input.wav --size 12 --rpm 33
  python record_generator.py --audio input.wav --size 7  --rpm 45 --groove stereo --sides 2
  python record_generator.py --help

Output modes (--output-mode)
─────────────────────────────
  single    : all triangles collected in RAM → one STL file  [default for small discs]
  streaming : triangles written to disk as generated         [default for large discs / both sides]

Language (--lang)
──────────────────
  en  : English  [default]
  ja  : Japanese
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# ── Project modules ───────────────────────────────────────────────────────
from record_specs import (
    RECORD_SPECS, RPM_GROOVE,
    GROOVE_SAMPLE_RATE, GROOVE_PTS_PER_REV,
    STL_MODE_SINGLE, STL_MODE_STREAMING,
    calc_max_duration,
)
from stl_writer import (
    get_print_recommendations, estimate_triangle_count,
)
from reverse_record.stl_to_audio import load_stl_mesh, write_3mf

# Try to import Rust bridge
try:
    from rust_bridge import get_bridge
    HAS_RUST = True
except (ImportError, FileNotFoundError):
    HAS_RUST = False


# ══════════════════════════════════════════════════════════════════════════════
# i18n strings
# ══════════════════════════════════════════════════════════════════════════════

_LANG: dict[str, dict[str, str]] = {
    "en": {
        "title":          "=== 3D Vinyl Record Generator ===",
        "loading":        "Loading audio…",
        "resampling":     "Resampling…",
        "riaa":           "Applying RIAA pre-emphasis…",
        "normalise":      "Normalising signal…",
        "generating_a":   "Calculating groove path  [Side A]…",
        "generating_b":   "Calculating groove path  [Side B]…",
        "building_stl_a": "Building STL  [Side A]…",
        "building_stl_b": "Building STL  [Side B]…",
        "writing":        "Writing STL file…",
        "done":           "Done.",
        "warn_long":      "WARNING: audio exceeds max duration — will be trimmed.",
        "warn_numpy":     "numpy not found; install with: pip install numpy",
        "warn_pydub":     "pydub not found; MP3 support disabled. Install: pip install pydub",
        "error":          "ERROR",
        "sep":            "─" * 52,
        "hdr_spec":       "Record specifications",
        "hdr_audio":      "Audio",
        "hdr_groove":     "Groove",
        "hdr_stl":        "STL output",
        "hdr_print":      "Recommended print settings",
        "size":           "Size",
        "rpm":            "RPM",
        "groove_mode":    "Groove mode",
        "sides":          "Sides",
        "max_dur":        "Max recordable duration",
        "actual_dur":     "Audio duration",
        "turns":          "Groove turns",
        "groove_len":     "Groove length",
        "groove_pts":     "Groove points",
        "tri_count":      "Triangles",
        "stl_size":       "STL file size",
        "stl_file":       "Output file",
        "elapsed":        "Elapsed",
        "layer_h":        "Layer height",
        "nozzle":         "Nozzle diameter",
        "speed":          "Print speed",
        "material":       "Material",
        "infill":         "Infill",
        "note":           "Note",
    },
    "ja": {
        "title":          "=== 3D ビニールレコード生成ツール ===",
        "loading":        "音声ファイルを読み込み中…",
        "resampling":     "リサンプリング中…",
        "riaa":           "RIAA 等化フィルター適用中…",
        "normalise":      "信号を正規化中…",
        "generating_a":   "溝パスを計算中 [A面]…",
        "generating_b":   "溝パスを計算中 [B面]…",
        "building_stl_a": "STL を構築中 [A面]…",
        "building_stl_b": "STL を構築中 [B面]…",
        "writing":        "STL ファイルを書き込み中…",
        "done":           "完了。",
        "warn_long":      "警告: 音声が最大収録時間を超えています — 切り詰めます。",
        "warn_numpy":     "numpy が見つかりません。pip install numpy でインストールしてください。",
        "warn_pydub":     "pydub が見つかりません。MP3 サポート無効。pip install pydub",
        "error":          "エラー",
        "sep":            "─" * 52,
        "hdr_spec":       "レコード仕様",
        "hdr_audio":      "音声",
        "hdr_groove":     "溝",
        "hdr_stl":        "STL 出力",
        "hdr_print":      "推奨印刷設定",
        "size":           "サイズ",
        "rpm":            "回転数",
        "groove_mode":    "溝方式",
        "sides":          "面数",
        "max_dur":        "最大収録時間",
        "actual_dur":     "音声時間",
        "turns":          "溝の巻き数",
        "groove_len":     "溝の総延長",
        "groove_pts":     "溝の点数",
        "tri_count":      "三角形数",
        "stl_size":       "STL ファイルサイズ",
        "stl_file":       "出力ファイル",
        "elapsed":        "処理時間",
        "layer_h":        "積層ピッチ",
        "nozzle":         "ノズル径",
        "speed":          "印刷速度",
        "material":       "材料",
        "infill":         "充填率",
        "note":           "注意",
    },
}

def _t(key: str, lang: str = "en") -> str:
    return _LANG.get(lang, _LANG["en"]).get(key, key)


# ══════════════════════════════════════════════════════════════════════════════
# Progress callback factory
# ══════════════════════════════════════════════════════════════════════════════

def _make_progress(lang: str):
    """
    Returns a progress callback suitable for passing to pipeline functions.
    Prints to stdout with a carriage-return-style progress bar.
    """
    last_pct = [-1]

    def cb(msg: str, pct: int):
        if pct != last_pct[0]:
            bar_w  = 30
            filled = int(bar_w * pct / 100)
            bar    = "█" * filled + "░" * (bar_w - filled)
            print(f"\r  [{bar}] {pct:3d}%  {msg:<45}", end="", flush=True)
            last_pct[0] = pct
        if pct >= 100:
            print()  # newline at completion

    return cb


# ══════════════════════════════════════════════════════════════════════════════
# Duration formatting
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_sec(s: float) -> str:
    m   = int(s) // 60
    sec = int(s) % 60
    return f"{m}:{sec:02d}"

def _fmt_size(mb: float) -> str:
    if mb >= 1000:
        return f"{mb/1024:.2f} GB"
    return f"{mb:.1f} MB"


# ══════════════════════════════════════════════════════════════════════════════
# Report helpers
# ══════════════════════════════════════════════════════════════════════════════

def _print_report(
    *,
    lang:       str,
    size_inch:  int,
    rpm:        int,
    groove_mode:str,
    sides:      int,
    max_dur:    float,
    actual_dur: float,
    turns:      float,
    groove_len: float,
    groove_pts: int,
    stl_files:  List[str],
    tri_counts: List[int],
    elapsed_s:  float,
) -> None:
    T = lambda k: _t(k, lang)
    spec = RECORD_SPECS[size_inch]
    pr   = get_print_recommendations(rpm)

    sep = T("sep")
    print(f"\n{sep}")
    print(f"  {T('hdr_spec')}")
    print(sep)
    print(f"  {T('size'):<22}: {size_inch}\" ({spec['outer_r']*2:.0f} mm diameter)")
    print(f"  {T('rpm'):<22}: {rpm}")
    print(f"  {T('groove_mode'):<22}: {groove_mode}")
    print(f"  {T('sides'):<22}: {sides}")

    print(f"\n  {T('hdr_audio')}")
    print(f"  {T('max_dur'):<22}: {_fmt_sec(max_dur)}  ({max_dur:.1f} s)")
    print(f"  {T('actual_dur'):<22}: {_fmt_sec(actual_dur)}  ({actual_dur:.1f} s)")

    print(f"\n  {T('hdr_groove')}")
    print(f"  {T('turns'):<22}: {turns:.1f}")
    print(f"  {T('groove_len'):<22}: {groove_len/1000:.2f} m")
    print(f"  {T('groove_pts'):<22}: {groove_pts:,}")

    print(f"\n  {T('hdr_stl')}")
    total_tris = sum(tri_counts)
    total_mb   = sum(
        (80 + 4 + n * 50) / (1024 * 1024) for n in tri_counts
    )
    print(f"  {T('tri_count'):<22}: {total_tris:,}")
    print(f"  {T('stl_size'):<22}: {_fmt_size(total_mb)}")
    for f in stl_files:
        sz = os.path.getsize(f) / (1024 * 1024) if os.path.exists(f) else 0
        print(f"  {T('stl_file'):<22}: {f}  ({_fmt_size(sz)})")

    print(f"\n  {T('hdr_print')}")
    print(f"  {T('layer_h'):<22}: {pr['layer_height_mm']} mm")
    print(f"  {T('nozzle'):<22}: ≤ {pr['nozzle_mm']} mm")
    print(f"  {T('speed'):<22}: {pr['speed_mm_s']} mm/s")
    print(f"  {T('material'):<22}: {pr['material']}")
    print(f"  {T('infill'):<22}: {pr['infill_pct']}%")
    print(f"  {T('note'):<22}: {pr['note']}")

    print(f"\n  {T('elapsed'):<22}: {elapsed_s:.1f} s")
    print(sep)


def _save_json_report(
    *,
    size_inch:   int,
    rpm:         int,
    groove_mode: str,
    sides:       int,
    max_dur:     float,
    actual_dur:  float,
    turns:       float,
    groove_len:  float,
    groove_pts:  int,
    stl_files:   List[str],
    tri_counts:  List[int],
    output_path: str,
    split_mode:  str = "duplicate",
    multi_files_mode: str = "auto",
    side_durations: Optional[List[float]] = None,
    groove_spacing_factor: float = 1.0,
    multi_files_count: int = 0,
    multi_files_a_count: int = 0,
    multi_files_b_count: int = 0,
    silence_between: float = 0.0,
    silence_between_a: float = 0.0,
    silence_between_b: float = 0.0,
) -> str:
    spec = RECORD_SPECS[size_inch]
    g    = RPM_GROOVE[rpm]
    pr   = get_print_recommendations(rpm)
    data = {
        "generated_at": datetime.now().isoformat(),
        "record": {
            "size_inch":    size_inch,
            "diameter_mm":  spec["outer_r"] * 2,
            "thickness_mm": spec["thickness"],
            "rpm":          rpm,
            "groove_mode":  groove_mode,
            "sides":        sides,
            "split_mode":   split_mode,
        },
        "groove": {
            "width_mm":      g["groove_width"],
            "depth_mm":      g["groove_depth"],
            "spacing_mm":    g["groove_spacing"],
            "spacing_factor": groove_spacing_factor,
            "turns":         round(turns, 2),
            "total_length_m":round(groove_len / 1000, 3),
            "total_points":  groove_pts,
        },
        "audio": {
            "max_duration_s":    round(max_dur, 2),
            "actual_duration_s": round(actual_dur, 2),
            "side_durations_s":  [round(v, 2) for v in (side_durations or [])],
            "multi_files_mode":   multi_files_mode,
            "multi_files_count": multi_files_count,
            "multi_files_a_count": multi_files_a_count,
            "multi_files_b_count": multi_files_b_count,
            "silence_between_s":   silence_between,
            "silence_between_a_s": silence_between_a,
            "silence_between_b_s": silence_between_b,
        },
        "stl_output": {
            "files":           stl_files,
            "triangle_counts": tri_counts,
            "total_triangles": sum(tri_counts),
        },
        "print_settings": {
            "layer_height_mm": pr["layer_height_mm"],
            "nozzle_mm":       pr["nozzle_mm"],
            "speed_mm_s":      pr["speed_mm_s"],
            "infill_percent":  pr["infill_pct"],
            "material":        pr["material"],
            "recommended_printer": "SLA/DLP",
        },
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return output_path


# ══════════════════════════════════════════════════════════════════════════════
# Groove calculation wrapper (tries Rust first, falls back to Python)
# ══════════════════════════════════════════════════════════════════════════════

def _convert_to_wav(source_path: str) -> str:
    path = Path(source_path)
    if path.suffix.lower() == ".wav":
        return source_path

    wav_path = path.with_suffix(".wav")
    afconvert = shutil.which("afconvert")
    if afconvert:
        proc = subprocess.run(
            [afconvert, str(path), str(wav_path), "-f", "WAVE", "-d", "LEI16"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and wav_path.is_file():
            return str(wav_path)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-acodec", "pcm_s16le", str(wav_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and wav_path.is_file():
            return str(wav_path)

    raise RuntimeError(
        f"Unable to convert '{source_path}' to WAV. Install ffmpeg or afconvert."
    )


def _ensure_wav_paths(paths: Optional[List[str]]) -> Optional[List[str]]:
    if paths is None:
        return None
    return [_convert_to_wav(p) for p in paths]


def _calculate_groove(
    left_ch: List[float],
    right_ch: List[float],
    size_inch: int,
    rpm: int,
    groove_mode: str,
    quality: str,
    groove_spacing_factor: float,
    sample_rate: int = GROOVE_SAMPLE_RATE,
    progress_cb = None,
) -> List[GroovePoint]:
    raise RuntimeError("Rust migration is complete; groove calculation is handled in Rust.")


# ══════════════════════════════════════════════════════════════════════════════
# Core pipeline
# ══════════════════════════════════════════════════════════════════════════════

def run(
    audio_path:  str,
    size_inch:   int,
    rpm:         int,
    groove_mode: str,
    sides:       int,
    output_prefix: str,
    output_mode: str,       # STL_MODE_SINGLE or STL_MODE_STREAMING
    output_format: str,     # 'stl', '3mf', or 'both'
    quality:     str,       # key in GROOVE_PTS_PER_REV
    apply_riaa:  bool,
    lang:        str,
    split_mode:  str = "duplicate",
    groove_spacing_factor: float = 1.0,
    multi_files_mode: str = "auto",
    multi_audio_paths: Optional[List[str]] = None,
    multi_audio_paths_a: Optional[List[str]] = None,
    multi_audio_paths_b: Optional[List[str]] = None,
    silence_between: float = 0.0,
    silence_between_a: float = 0.0,
    silence_between_b: float = 0.0,
) -> int:
    """
    Full pipeline. Returns 0 on success, 1 on error.
    """
    T = lambda k: _t(k, lang)

    if not HAS_RUST:
        print(f"\n{T('error')}: Rust backend unavailable. Build the Rust binary.", file=sys.stderr)
        return 1

    if not os.path.isfile(audio_path):
        print(f"\n{T('error')}: file not found: {audio_path}", file=sys.stderr)
        return 1

    try:
        main_audio = _convert_to_wav(audio_path)
        audio_paths = _ensure_wav_paths(multi_audio_paths)
        audio_paths_a = _ensure_wav_paths(multi_audio_paths_a)
        audio_paths_b = _ensure_wav_paths(multi_audio_paths_b)
    except Exception as exc:
        print(f"\n{T('error')}: {exc}", file=sys.stderr)
        return 1

    output_mode_str = "streaming" if output_mode == STL_MODE_STREAMING else "single"

    try:
        bridge = get_bridge()
        bridge.run(
            audio_path=main_audio,
            size_inch=size_inch,
            rpm=rpm,
            groove_mode=groove_mode,
            sides=sides,
            output_prefix=output_prefix,
            output_mode=output_mode_str,
            quality=quality,
            apply_riaa=apply_riaa,
            split_mode=split_mode,
            groove_spacing_factor=groove_spacing_factor,
            multi_files_mode=multi_files_mode,
            audio_paths=audio_paths,
            audio_paths_a=audio_paths_a,
            audio_paths_b=audio_paths_b,
            silence_between=silence_between,
            silence_between_a=silence_between_a,
            silence_between_b=silence_between_b,
        )
    except Exception as exc:
        print(f"\n{T('error')}: {exc}", file=sys.stderr)
        return 1

    report_path = Path(f"{output_prefix}_report.json")
    if not report_path.is_file():
        print(f"\n{T('error')}: report JSON was not created", file=sys.stderr)
        return 1

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        stl_files = report.get("stl_output", {}).get("files", [])
        threemf_files: List[str] = []
        if output_format in ("3mf", "both"):
            for stl_file in stl_files:
                stl_path = Path(stl_file)
                if stl_path.is_file():
                    threemf_path = stl_path.with_suffix(".3mf")
                    triangles = load_stl_mesh(stl_path)
                    write_3mf(threemf_path, triangles)
                    threemf_files.append(str(threemf_path))
            if threemf_files:
                report["3mf_output"] = {"files": threemf_files}
                report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"\n{T('done')}")
        print(f"Report written to: {report_path}")

        if stl_files:
            print("STL files:")
            for path in stl_files:
                print(f"  - {path}")
        if threemf_files:
            print("3MF files:")
            for path in threemf_files:
                print(f"  - {path}")
    except Exception as exc:
        print(f"\n{T('error')}: failed to produce 3MF output: {exc}", file=sys.stderr)
        return 1

    return 0


# ══════════════════════════════════════════════════════════════════════════════
# CLI argument parser
# ══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="record_generator.py",
        description=(
            "Generate a 3D-printable vinyl record STL from an audio file.\n"
            "音声ファイルから3Dプリンター用ビニールレコードSTLを生成します。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python record_generator.py --audio song.wav --size 12 --rpm 33
  python record_generator.py --audio song.wav --size 7  --rpm 45 --groove stereo --sides 2
  python record_generator.py --audio song.wav --quality max --output-mode streaming
""",
    )

    # Required (unless --info)
    p.add_argument(
        "--audio", "-a", required=False, default=None, metavar="FILE",
        help="Input audio file (WAV or MP3). Required unless --info is used.",
    )

    # Record geometry
    p.add_argument(
        "--size", "-s", type=int, choices=[7, 10, 12], default=12,
        metavar="{7,10,12}",
        help="Record size in inches (default: 12).",
    )
    p.add_argument(
        "--rpm", "-r", type=int, choices=[33, 45, 78], default=33,
        metavar="{33,45,78}",
        help="Rotation speed in RPM (default: 33).",
    )
    p.add_argument(
        "--groove", "-g", choices=["mono", "stereo"], default="mono",
        help="Groove modulation mode: mono (default) or stereo (IEC 45/45).",
    )
    p.add_argument(
        "--sides", type=int, choices=[1, 2], default=1,
        help="Number of sides: 1 (default) or 2 (Side A + Side B).",
    )
    p.add_argument(
        "--split-mode", choices=["duplicate", "auto"], default="duplicate",
        help=(
            "Two-sided behavior: duplicate writes the same audio to A/B; "
            "auto splits audio that exceeds one side onto Side B."
        ),
    )

    # Quality
    p.add_argument(
        "--quality", "-q",
        choices=list(GROOVE_PTS_PER_REV.keys()),
        default="high",
        help=(
            "Groove point density / max frequency:\n"
            "  preview  ~200 Hz   (fast preview)\n"
            "  draft    ~1 kHz\n"
            "  high     ~2 kHz    [default]\n"
            "  full     ~4 kHz\n"
            "  max      ~10 kHz   (large STL, slow)\n"
        ),
    )

    # Output
    p.add_argument(
        "--output", "-o", metavar="PREFIX",
        help=(
            "Output file prefix (default: derived from audio filename).\n"
            "Each side is written as <prefix>_sideA.stl, <prefix>_sideB.stl.\n"
            "A JSON report is always written as <prefix>_report.json."
        ),
    )
    p.add_argument(
        "--output-format", choices=["stl", "3mf", "both"], default="stl",
        dest="output_format",
        help=(
            "Output format for record geometry.\n"
            "  stl   – generate STL files only (default).\n"
            "  3mf   – generate 3MF files converted from STL.\n"
            "  both  – generate both STL and 3MF files."
        ),
    )
    p.add_argument(
        "--output-mode", choices=["single", "streaming"], default="single",
        dest="output_mode",
        help=(
            "STL output strategy:\n"
            "  single    – collect all triangles in RAM, then write (default).\n"
            "  streaming – write triangles directly to disk (low RAM usage).\n"
            "              Recommended for 'full' or 'max' quality, or both sides."
        ),
    )

    # Audio
    p.add_argument(
        "--no-riaa", action="store_true",
        help="Skip RIAA pre-emphasis equalisation (not recommended).",
    )

    # Language
    p.add_argument(
        "--lang", "-l", choices=["en", "ja"], default="en",
        help="Output language: en (default) or ja.",
    )

    # Info
    p.add_argument(
        "--info", action="store_true",
        help="Print estimated disc stats and exit (no STL generated).",
    )

    return p


# ══════════════════════════════════════════════════════════════════════════════
# Info-only mode
# ══════════════════════════════════════════════════════════════════════════════

def _print_info(size_inch: int, rpm: int, quality: str, lang: str) -> None:
    T     = lambda k: _t(k, lang)
    stats = calc_max_duration(size_inch, rpm)
    est   = estimate_triangle_count(size_inch, rpm, GROOVE_PTS_PER_REV[quality])
    spec  = RECORD_SPECS[size_inch]
    sep   = T("sep")

    print(f"\n{sep}")
    print(f"  {T('hdr_spec')}  [info only, no STL generated]")
    print(sep)
    print(f"  {T('size'):<22}: {size_inch}\" ({spec['outer_r']*2:.0f} mm)")
    print(f"  {T('rpm'):<22}: {rpm}")
    print(f"  {T('max_dur'):<22}: {_fmt_sec(stats['duration_s'])}")
    print(f"  {T('turns'):<22}: {stats['turns']:.1f}")
    print(f"  {T('groove_len'):<22}: {stats['groove_len_mm']/1000:.2f} m")
    print(f"  quality                : {quality}  ({GROOVE_PTS_PER_REV[quality]} pts/rev)")
    print(f"  {T('groove_pts'):<22}: {est['groove_pts']:,}")
    print(f"  {T('tri_count'):<22}: {est['total_tris']:,}  "
          f"(disc {est['disc_tris']:,} + groove {est['groove_tris']:,})")
    print(f"  {T('stl_size'):<22}: {_fmt_size(est['stl_size_mb'])}  (estimate)")
    print(sep)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()
    lang   = args.lang

    print(_t("title", lang))

    # Info-only mode
    if args.info:
        _print_info(args.size, args.rpm, args.quality, lang)
        sys.exit(0)

    # Validate audio file
    if not args.audio:
        print(f"\n{_t('error', lang)}: --audio is required when not using --info.", file=sys.stderr)
        parser.print_usage(sys.stderr)
        sys.exit(1)
    if not os.path.isfile(args.audio):
        print(f"\n{_t('error', lang)}: file not found: {args.audio}", file=sys.stderr)
        sys.exit(1)

    # Determine output prefix
    prefix = args.output or os.path.splitext(args.audio)[0]

    # Map CLI flag to constant
    output_mode = STL_MODE_STREAMING if args.output_mode == "streaming" else STL_MODE_SINGLE

    # Warn if high-quality + both sides with single mode
    pts = GROOVE_PTS_PER_REV[args.quality]
    stats = calc_max_duration(args.size, args.rpm)
    est_mb = (80 + 4 + int(stats["turns"] * pts) * 4 * 50) / 1e6
    if args.sides == 2:
        est_mb *= 2
    if est_mb > 500 and output_mode == STL_MODE_SINGLE:
        print(
            f"\n  [WARN] Estimated STL size is ~{est_mb/1024:.1f} GB. "
            "Consider --output-mode streaming to reduce peak RAM usage."
        )

    sys.exit(
        run(
            audio_path=args.audio,
            size_inch=args.size,
            rpm=args.rpm,
            groove_mode=args.groove,
            sides=args.sides,
            output_prefix=prefix,
            output_mode=output_mode,
            output_format=args.output_format,
            quality=args.quality,
            apply_riaa=not args.no_riaa,
            lang=args.lang,
            split_mode=args.split_mode,
        )
    )


if __name__ == "__main__":
    main()
