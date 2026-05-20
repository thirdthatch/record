#!/usr/bin/env python3
"""
Local web bridge for record_gui.html.

Run:
  python3 gui_server.py

Then open:
  http://127.0.0.1:8765/
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

warnings.filterwarnings("ignore", category=DeprecationWarning)
import cgi

from record_generator import run
from record_specs import STL_MODE_SINGLE, STL_MODE_STREAMING


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "reverse_record"))

from stl_to_audio import (
    load_geometry,
    unique_vertices,
    side_vertices_by_top_z,
    side_triangles_by_top_z,
    choose_size_inch,
    infer_size_rpm_quality,
    reconstruct_waveform,
    write_wav,
    is_stereo,
)

UPLOAD_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "outputs"
HOST = "127.0.0.1"
PORT = 8765


def _safe_name(name: str) -> str:
    base = Path(name or "audio.wav").name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return safe or "audio.wav"


def _field_text(form: cgi.FieldStorage, name: str, default: str) -> str:
    item = form[name] if name in form else None
    if item is None:
        return default
    value = item.value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _prepare_audio_for_python(upload_path: Path) -> tuple[Path, str]:
    """Return a WAV path for the Python generator, converting uploads when needed."""
    if upload_path.suffix.lower() == ".wav":
        return upload_path, ""

    wav_path = upload_path.with_suffix(".wav")
    afconvert = shutil.which("afconvert")
    if afconvert:
        proc = subprocess.run(
            [afconvert, str(upload_path), str(wav_path), "-f", "WAVE", "-d", "LEI16"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode == 0 and wav_path.is_file():
            return wav_path, f"Converted {upload_path.name} to WAV with afconvert.\n"
        raise RuntimeError(
            "MP3/WAV conversion failed with afconvert.\n"
            f"{proc.stdout}{proc.stderr}"
        )

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(upload_path), "-acodec", "pcm_s16le", str(wav_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode == 0 and wav_path.is_file():
            return wav_path, f"Converted {upload_path.name} to WAV with ffmpeg.\n"
        raise RuntimeError(
            "MP3/WAV conversion failed with ffmpeg.\n"
            f"{proc.stdout}{proc.stderr}"
        )

    raise RuntimeError(
        "MP3 input needs conversion before Python can process it. "
        "Install pydub+ffmpeg or use WAV input."
    )


class GUIHandler(SimpleHTTPRequestHandler):
    server_version = "RecordGUI/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/":
            self._send_file(ROOT / "record_gui.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/outputs/"):
            rel = unquote(parsed.path.removeprefix("/outputs/"))
            self._send_output(rel)
            return
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/generate":
            self._handle_generate()
            return
        if parsed.path == "/api/reverse_record":
            self._handle_reverse_record()
            return
        self.send_error(404, "Not found")

    def _handle_generate(self) -> None:
        ctype, _ = cgi.parse_header(self.headers.get("Content-Type", ""))
        if ctype != "multipart/form-data":
            self._send_json({"ok": False, "error": "multipart/form-data required"}, status=400)
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )

        if "audio" not in form or not getattr(form["audio"], "filename", ""):
            self._send_json({"ok": False, "error": "audio file is required"}, status=400)
            return

        try:
            size = int(_field_text(form, "size", "12"))
            rpm = int(_field_text(form, "rpm", "33"))
            groove = _field_text(form, "groove", "mono")
            sides = int(_field_text(form, "sides", "1"))
            quality = _field_text(form, "quality", "high")
            output_mode_text = _field_text(form, "output_mode", "single")
            split_mode = _field_text(form, "split_mode", "duplicate")
            multi_files_mode = _field_text(form, "multi_files_mode", "auto")
            apply_riaa = _field_text(form, "apply_riaa", "1") == "1"
            lang = _field_text(form, "lang", "ja")
            groove_spacing_factor = float(_field_text(form, "groove_spacing_factor", "1.0"))
            multi_files_count = int(_field_text(form, "multi_files_count", "0"))
            multi_files_a_count = int(_field_text(form, "multi_files_a_count", "0"))
            multi_files_b_count = int(_field_text(form, "multi_files_b_count", "0"))
            silence_between = float(_field_text(form, "silence_between", "0.0"))
            silence_between_a = float(_field_text(form, "silence_between_a", "0.0"))
            silence_between_b = float(_field_text(form, "silence_between_b", "0.0"))
        except ValueError as exc:
            self._send_json({"ok": False, "error": f"invalid option: {exc}"}, status=400)
            return

        if size not in {7, 10, 12} or rpm not in {33, 45, 78}:
            self._send_json({"ok": False, "error": "invalid record size or rpm"}, status=400)
            return
        if groove not in {"mono", "stereo"} or sides not in {1, 2}:
            self._send_json({"ok": False, "error": "invalid groove mode or sides"}, status=400)
            return
        if split_mode not in {"duplicate", "auto"}:
            self._send_json({"ok": False, "error": "invalid split mode"}, status=400)
            return
        if multi_files_mode not in {"auto", "separate", "none"}:
            self._send_json({"ok": False, "error": "invalid multi_files_mode"}, status=400)
            return
        if not 0.5 <= groove_spacing_factor <= 2.0:
            self._send_json({"ok": False, "error": "groove_spacing_factor must be between 0.5 and 2.0"}, status=400)
            return

        UPLOAD_DIR.mkdir(exist_ok=True)
        OUTPUT_DIR.mkdir(exist_ok=True)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        audio_item = form["audio"]
        original_name = _safe_name(audio_item.filename)
        upload_path = UPLOAD_DIR / f"{stamp}_{original_name}"
        with upload_path.open("wb") as out:
            shutil.copyfileobj(audio_item.file, out)

        try:
            audio_path, conversion_log = _prepare_audio_for_python(upload_path)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        # Handle multiple files
        multi_audio_paths = []
        multi_audio_paths_a = []
        multi_audio_paths_b = []
        if multi_files_mode == "auto":
            for idx in range(multi_files_count):
                key = f"audio_{idx}"
                if key in form and getattr(form[key], "filename", ""):
                    multi_item = form[key]
                    multi_name = _safe_name(multi_item.filename)
                    multi_path = UPLOAD_DIR / f"{stamp}_{idx}_{multi_name}"
                    with multi_path.open("wb") as out:
                        shutil.copyfileobj(multi_item.file, out)
                    try:
                        wav_path, _ = _prepare_audio_for_python(multi_path)
                        multi_audio_paths.append(str(wav_path))
                    except Exception:
                        pass
        elif multi_files_mode == "separate":
            for idx in range(multi_files_a_count):
                key = f"audio_a_{idx}"
                if key in form and getattr(form[key], "filename", ""):
                    multi_item = form[key]
                    multi_name = _safe_name(multi_item.filename)
                    multi_path = UPLOAD_DIR / f"{stamp}_A_{idx}_{multi_name}"
                    with multi_path.open("wb") as out:
                        shutil.copyfileobj(multi_item.file, out)
                    try:
                        wav_path, _ = _prepare_audio_for_python(multi_path)
                        multi_audio_paths_a.append(str(wav_path))
                    except Exception:
                        pass
            for idx in range(multi_files_b_count):
                key = f"audio_b_{idx}"
                if key in form and getattr(form[key], "filename", ""):
                    multi_item = form[key]
                    multi_name = _safe_name(multi_item.filename)
                    multi_path = UPLOAD_DIR / f"{stamp}_B_{idx}_{multi_name}"
                    with multi_path.open("wb") as out:
                        shutil.copyfileobj(multi_item.file, out)
                    try:
                        wav_path, _ = _prepare_audio_for_python(multi_path)
                        multi_audio_paths_b.append(str(wav_path))
                    except Exception:
                        pass

        prefix = OUTPUT_DIR / f"{upload_path.stem}_{size}in_{rpm}rpm"
        output_mode = STL_MODE_STREAMING if output_mode_text == "streaming" else STL_MODE_SINGLE
        output_format = _field_text(form, "output_format", "stl")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run(
                audio_path=str(audio_path),
                size_inch=size,
                rpm=rpm,
                groove_mode=groove,
                sides=sides,
                output_prefix=str(prefix),
                output_mode=output_mode,
            output_format=output_format,
                apply_riaa=apply_riaa,
                lang=lang,
                split_mode=split_mode,
                groove_spacing_factor=groove_spacing_factor,
                multi_files_mode=multi_files_mode,
                multi_audio_paths=multi_audio_paths if multi_files_mode == "auto" else None,
                multi_audio_paths_a=multi_audio_paths_a if multi_files_mode == "separate" else None,
                multi_audio_paths_b=multi_audio_paths_b if multi_files_mode == "separate" else None,
                silence_between=silence_between,
                silence_between_a=silence_between_a,
                silence_between_b=silence_between_b,
            )

        log = conversion_log + stdout.getvalue() + stderr.getvalue()
        report_path = Path(f"{prefix}_report.json")
        if code != 0:
            self._send_json({"ok": False, "error": "Record generation failed", "log": log}, status=500)
            return
        if not report_path.is_file():
            self._send_json({"ok": False, "error": "report JSON was not created", "log": log}, status=500)
            return

        report = json.loads(report_path.read_text(encoding="utf-8"))
        files = []
        for path_text in report.get("stl_output", {}).get("files", []):
            path = Path(path_text)
            if path.is_file():
                files.append({
                    "name": path.name,
                    "url": f"/outputs/{path.name}",
                    "bytes": path.stat().st_size,
                })
        for path_text in report.get("3mf_output", {}).get("files", []):
            path = Path(path_text)
            if path.is_file():
                files.append({
                    "name": path.name,
                    "url": f"/outputs/{path.name}",
                    "bytes": path.stat().st_size,
                })
        files.append({
            "name": report_path.name,
            "url": f"/outputs/{report_path.name}",
            "bytes": report_path.stat().st_size,
        })

        self._send_json({"ok": True, "report": report, "files": files, "log": log})

    def _handle_reverse_record(self) -> None:
        """Process STL upload and convert to audio via reverse_record tool."""
        ctype, _ = cgi.parse_header(self.headers.get("Content-Type", ""))
        if ctype != "multipart/form-data":
            self._send_json({"ok": False, "error": "multipart/form-data required"}, status=400)
            return

        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                },
            )

            if "file" not in form or not getattr(form["file"], "filename", ""):
                self._send_json({"ok": False, "error": "STL file is required"}, status=400)
                return

            inverse_riaa = _field_text(form, "inverse_riaa", "0") == "1"

            UPLOAD_DIR.mkdir(exist_ok=True)
            OUTPUT_DIR.mkdir(exist_ok=True)

            stamp = time.strftime("%Y%m%d_%H%M%S")
            stl_item = form["file"]
            original_name = _safe_name(stl_item.filename)
            upload_path = UPLOAD_DIR / f"{stamp}_{original_name}"
            
            with upload_path.open("wb") as out:
                shutil.copyfileobj(stl_item.file, out)

            if upload_path.suffix.lower() not in {".stl", ".3mf"}:
                self._send_json({"ok": False, "error": "Only STL and 3MF files are supported"}, status=400)
                return

            vertices, _ = load_geometry(upload_path)
            sides = sorted(side_vertices_by_top_z(vertices), key=lambda item: item[0])
            
            if not sides:
                raise ValueError("Unable to extract groove geometry from STL")

            output_files = []
            record_info = None
            waveform_left = []
            waveform_right = []
            waveform_channels = 1
            waveform_sample_rate = 44100

            for idx, (top_z, side_points) in enumerate(sides):
                from record_specs import RECORD_SPECS, RPM_GROOVE
                
                side_name = "A" if idx == 0 else "B"
                r_outer = max(
                    (pt[0]**2 + pt[1]**2)**0.5 for pt in side_points
                )
                size_inch = choose_size_inch(r_outer)
                rpm, quality, factor, pitch = infer_size_rpm_quality(side_points, size_inch)
                stereo_flag = is_stereo(
                    side_points,
                    top_z,
                    RECORD_SPECS[size_inch]["thickness"],
                    RPM_GROOVE[rpm]["groove_depth"],
                )
                
                left, right, sample_rate, stereo_flag = reconstruct_waveform(
                    side_points,
                    size_inch,
                    rpm,
                    quality,
                    factor,
                    side_name,
                    apply_inverse_riaa=inverse_riaa,
                )

                # Save WAV
                wav_name = f"{upload_path.stem}_{side_name}.wav"
                wav_path = OUTPUT_DIR / wav_name
                write_wav(wav_path, left, right, sample_rate)
                output_files.append({
                    "name": wav_name,
                    "url": f"/outputs/{wav_name}",
                    "size": wav_path.stat().st_size,
                })

                # Prepare record info and waveform for first side
                if idx == 0:
                    duration_s = len(left) / sample_rate
                    record_info = {
                        "size_inch": size_inch,
                        "rpm": rpm,
                        "quality": quality,
                        "groove_spacing_factor": factor,
                        "groove_type": "stereo" if stereo_flag else "mono",
                        "channels": 2 if stereo_flag else 1,
                        "duration_s": duration_s,
                    }

                    waveform_sample_rate = sample_rate
                    waveform_channels = 2 if stereo_flag else 1
                    max_samples = 4000
                    step = max(1, len(left) // max_samples)
                    waveform_left = left[::step]
                    waveform_right = right[::step]

            if not record_info:
                raise ValueError("No sides extracted from STL")

            self._send_json({
                "ok": True,
                "info": record_info,
                "wav_url": output_files[0]["url"] if output_files else None,
                "downloads": output_files,
                "waveform": {
                    "left": waveform_left,
                    "right": waveform_right,
                    "sample_rate": waveform_sample_rate,
                    "channels": waveform_channels,
                },
            })
        except Exception as exc:
            import traceback
            self._send_json({
                "ok": False,
                "error": str(exc),
                "trace": traceback.format_exc(),
            }, status=500)

    def _send_output(self, rel: str) -> None:
        target = (OUTPUT_DIR / rel).resolve()
        try:
            target.relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            self.send_error(403, "Forbidden")
            return
        if not target.is_file():
            self.send_error(404, "File not found")
            return
        content_type = "application/json" if target.suffix == ".json" else "application/octet-stream"
        self._send_file(target, content_type, attachment=True)

    def _send_file(self, path: Path, content_type: str, attachment: bool = False) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    os.chdir(ROOT)
    UPLOAD_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), GUIHandler)
    print(f"Record GUI server: http://{HOST}:{PORT}/")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
