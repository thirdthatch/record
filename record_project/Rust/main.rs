// main.rs
// -------
// CLI entry point for Rust vinyl record generation.
// Reads JSON from stdin, processes via Rust modules, outputs JSON to stdout.

use std::fs::File;
use std::io::{self, Read, Write};

use serde_json::{json, Value};

mod specs;
mod groove;
mod stl;
mod resample;
mod riaa;

const TARGET_SR: u32 = 44100;

fn main() -> io::Result<()> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;

    let request: Value = match serde_json::from_str(&input) {
        Ok(v) => v,
        Err(e) => {
            let error = json!({ "ok": false, "error": format!("JSON parse error: {}", e) });
            println!("{}", error.to_string());
            return Ok(());
        }
    };

    let response = match request.get("command").and_then(|c| c.as_str()) {
        Some("run") => run_handler(&request),
        Some("calculate_groove") => calculate_groove_handler(&request),
        Some("resample_audio") => resample_audio_handler(&request),
        Some("apply_riaa") => apply_riaa_handler(&request),
        _ => json!({ "ok": false, "error": "Unknown command" }),
    };

    println!("{}", response.to_string());
    Ok(())
}

fn load_wav(path: &str) -> Result<(Vec<f32>, Vec<f32>, u32), String> {
    let mut reader = hound::WavReader::open(path)
        .map_err(|e| format!("Failed to open WAV '{}': {}", path, e))?;
    let spec = reader.spec();
    let sample_rate = spec.sample_rate;
    let channels = spec.channels as usize;

    let samples: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => match spec.bits_per_sample {
            32 => reader
                .samples::<f32>()
                .map(|s| s.map_err(|e| e.to_string()))
                .collect::<Result<_, _>>()?,
            bits => {
                return Err(format!("Unsupported float bit depth: {}", bits));
            }
        },
        hound::SampleFormat::Int => match spec.bits_per_sample {
            8 => reader
                .samples::<i8>()
                .map(|s| s.map(|v| (v as f32) / 128.0).map_err(|e| e.to_string()))
                .collect::<Result<_, _>>()?,
            16 => reader
                .samples::<i16>()
                .map(|s| s.map(|v| v as f32 / 32768.0).map_err(|e| e.to_string()))
                .collect::<Result<_, _>>()?,
            24 => reader
                .samples::<i32>()
                .map(|s| s.map(|v| v as f32 / 8_388_608.0).map_err(|e| e.to_string()))
                .collect::<Result<_, _>>()?,
            32 => reader
                .samples::<i32>()
                .map(|s| s.map(|v| v as f32 / 2_147_483_648.0).map_err(|e| e.to_string()))
                .collect::<Result<_, _>>()?,
            bits => {
                return Err(format!("Unsupported int bit depth: {}", bits));
            }
        },
    };

    match channels {
        1 => Ok((samples.clone(), samples, sample_rate)),
        2 => {
            let frames = samples.len() / 2;
            let mut left = Vec::with_capacity(frames);
            let mut right = Vec::with_capacity(frames);
            for i in 0..frames {
                left.push(samples[2 * i]);
                right.push(samples[2 * i + 1]);
            }
            Ok((left, right, sample_rate))
        }
        n if n > 2 => {
            let frames = samples.len() / n;
            let mut left = Vec::with_capacity(frames);
            let mut right = Vec::with_capacity(frames);
            for i in 0..frames {
                let mut left_sum = 0.0_f32;
                let mut right_sum = 0.0_f32;
                let mut left_count = 0;
                let mut right_count = 0;
                for c in 0..n {
                    let sample = samples[i * n + c];
                    if c % 2 == 0 {
                        left_sum += sample;
                        left_count += 1;
                    } else {
                        right_sum += sample;
                        right_count += 1;
                    }
                }
                left.push(left_sum / left_count.max(1) as f32);
                right.push(right_sum / right_count.max(1) as f32);
            }
            Ok((left, right, sample_rate))
        }
        _ => Err(format!("Unsupported channel count: {}", channels)),
    }
}

fn prepare_audio_file(
    path: &str,
    apply_riaa: bool,
    max_duration_s: Option<f64>,
) -> Result<(Vec<f32>, Vec<f32>, f64), String> {
    let (mut left, mut right, mut sample_rate) = load_wav(path)?;
    let mut actual_dur = left.len() as f64 / sample_rate as f64;

    if let Some(max_dur) = max_duration_s {
        if actual_dur > max_dur {
            let clip = (max_dur * sample_rate as f64).ceil() as usize;
            left.truncate(clip);
            right.truncate(clip);
            actual_dur = max_dur;
        }
    }

    if sample_rate != TARGET_SR {
        left = resample::resample(&left, sample_rate, TARGET_SR);
        right = resample::resample(&right, sample_rate, TARGET_SR);
        sample_rate = TARGET_SR;
    }

    if apply_riaa {
        riaa::apply_riaa(&mut left, &mut right, sample_rate);
    }

    riaa::normalise_and_limit(&mut left, &mut right, 0.85);
    Ok((left, right, actual_dur))
}

fn create_silence(samples: usize) -> Vec<f32> {
    vec![0.0; samples]
}

fn concat_audio_chunks(
    chunks: Vec<(Vec<f32>, Vec<f32>, f64)>,
    silence_samples: usize,
) -> (Vec<f32>, Vec<f32>, f64) {
    let mut left = Vec::new();
    let mut right = Vec::new();
    let mut total_dur = 0.0;

    for (i, (mut l, mut r, dur)) in chunks.into_iter().enumerate() {
        if i > 0 && silence_samples > 0 {
            left.extend(create_silence(silence_samples));
            right.extend(create_silence(silence_samples));
            total_dur += silence_samples as f64 / TARGET_SR as f64;
        }
        left.append(&mut l);
        right.append(&mut r);
        total_dur += dur;
    }

    (left, right, total_dur)
}

fn load_audio_sequence(
    base_path: &str,
    additional_paths: &[String],
    silence_between: f64,
    apply_riaa: bool,
    max_duration_s: Option<f64>,
) -> Result<(Vec<f32>, Vec<f32>, f64), String> {
    let mut chunks = Vec::new();
    let first = prepare_audio_file(base_path, apply_riaa, max_duration_s)?;
    chunks.push(first);

    for path in additional_paths {
        let chunk = prepare_audio_file(path, apply_riaa, max_duration_s)?;
        chunks.push(chunk);
    }

    let silence_samples = (silence_between * TARGET_SR as f64).round() as usize;
    Ok(concat_audio_chunks(chunks, silence_samples))
}

fn load_separate_sides(
    primary_path: &str,
    a_paths: &[String],
    b_paths: &[String],
    silence_a: f64,
    silence_b: f64,
    apply_riaa: bool,
    max_duration_s: Option<f64>,
) -> Result<((Vec<f32>, Vec<f32>, f64), (Vec<f32>, Vec<f32>, f64)), String> {
    let a_chunk = if a_paths.is_empty() {
        prepare_audio_file(primary_path, apply_riaa, max_duration_s)?
    } else {
        load_audio_sequence(primary_path, a_paths, silence_a, apply_riaa, max_duration_s)?
    };

    let b_chunk = if b_paths.is_empty() {
        (Vec::new(), Vec::new(), 0.0)
    } else {
        // Use the first path in b_paths as the base, remaining entries as additional
        match b_paths.split_first() {
            Some((base, rest)) => load_audio_sequence(base.as_str(), rest, silence_b, apply_riaa, max_duration_s)?,
            None => (Vec::new(), Vec::new(), 0.0),
        }
    };

    Ok((a_chunk, b_chunk))
}

fn process_side(
    side_label: &str,
    left: &[f32],
    right: &[f32],
    output_path: &str,
    size_inch: u8,
    rpm: u8,
    groove_mode: &str,
    quality: &str,
    groove_spacing_factor: f64,
    output_mode: &str,
    side: &str,
) -> Result<(u32, usize), String> {
    let calc = groove::GrooveCalculator::new(
        size_inch,
        rpm,
        groove_mode,
        quality,
        groove_spacing_factor,
        side,
    );

    let groove_pts: Vec<_> = calc.generate(left, right, TARGET_SR);
    let point_count = groove_pts.len();

    let config = stl::StlConfig {
        size_inch,
        rpm,
        z_offset: 0.0,
        output_path: output_path.to_string(),
        streaming: output_mode == "streaming",
    };

    let triangles = if config.streaming {
        stl::write_stl_streaming(&config, &groove_pts, |_| {}).map_err(|e| e.to_string())?
    } else {
        stl::write_stl_single(&config, &groove_pts, |_| {}).map_err(|e| e.to_string())?
    };

    Ok((triangles, point_count))
}

fn run_handler(req: &Value) -> Value {
    let audio_path = req.get("audio_path").and_then(|v| v.as_str()).unwrap_or("");
    if audio_path.is_empty() {
        return json!({ "ok": false, "error": "audio_path is required" });
    }

    let size_inch = req.get("size_inch").and_then(|v| v.as_u64()).unwrap_or(12) as u8;
    let rpm = req.get("rpm").and_then(|v| v.as_u64()).unwrap_or(33) as u8;
    let groove_mode = req.get("groove_mode").and_then(|v| v.as_str()).unwrap_or("mono");
    let sides = req.get("sides").and_then(|v| v.as_u64()).unwrap_or(1) as u8;
    let quality = req.get("quality").and_then(|v| v.as_str()).unwrap_or("high");
    let output_prefix = req.get("output_prefix").and_then(|v| v.as_str()).unwrap_or("output");
    let output_mode = req.get("output_mode").and_then(|v| v.as_str()).unwrap_or("single");
    let apply_riaa = req.get("apply_riaa").and_then(|v| v.as_bool()).unwrap_or(true);
    let split_mode = req.get("split_mode").and_then(|v| v.as_str()).unwrap_or("duplicate");
    let groove_spacing_factor = req.get("groove_spacing_factor").and_then(|v| v.as_f64()).unwrap_or(1.0);
    let multi_files_mode = req.get("multi_files_mode").and_then(|v| v.as_str()).unwrap_or("auto");
    let silence_between = req.get("silence_between").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let silence_between_a = req.get("silence_between_a").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let silence_between_b = req.get("silence_between_b").and_then(|v| v.as_f64()).unwrap_or(0.0);

    let audio_paths: Vec<String> = req.get("audio_paths")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();

    let audio_paths_a: Vec<String> = req.get("audio_paths_a")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();

    let audio_paths_b: Vec<String> = req.get("audio_paths_b")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();

    let stats = specs::calc_max_duration(size_inch, rpm, groove_spacing_factor);
    let max_dur = stats.duration_s;

    let max_input_dur = if multi_files_mode == "separate" {
        max_dur
    } else if sides == 2 && split_mode == "auto" {
        max_dur * 2.0
    } else {
        max_dur
    };

    let mut left_a: Vec<f32>;
    let mut right_a: Vec<f32>;
    let mut left_b: Vec<f32> = Vec::new();
    let mut right_b: Vec<f32> = Vec::new();
    let mut actual_dur = 0.0;
    let mut side_durations = Vec::new();

    let samples_per_side = (max_dur * TARGET_SR as f64).round() as usize;

    match multi_files_mode {
        "separate" => {
            let ((a_left, a_right, a_dur), (b_left, b_right, b_dur)) = match load_separate_sides(
                audio_path,
                &audio_paths_a,
                &audio_paths_b,
                silence_between_a,
                silence_between_b,
                apply_riaa,
                Some(max_input_dur),
            ) {
                Ok(pair) => pair,
                Err(err) => return json!({ "ok": false, "error": err }),
            };

            left_a = a_left;
            right_a = a_right;
            left_b = b_left;
            right_b = b_right;
            actual_dur = a_dur + b_dur;
            side_durations.push(a_dur);
            if !left_b.is_empty() || !right_b.is_empty() {
                side_durations.push(b_dur);
            }
        }
        _ => {
            let (left, right, dur) = match load_audio_sequence(
                audio_path,
                &audio_paths,
                silence_between,
                apply_riaa,
                Some(max_input_dur),
            ) {
                Ok(tuple) => tuple,
                Err(err) => return json!({ "ok": false, "error": err }),
            };

            if sides == 2 && split_mode == "auto" {
                let split = left.len().min(samples_per_side);
                left_a = left[..split].to_vec();
                right_a = right[..split].to_vec();
                left_b = if left.len() > split { left[split..].to_vec() } else { Vec::new() };
                right_b = if right.len() > split { right[split..].to_vec() } else { Vec::new() };
                side_durations.push(left_a.len() as f64 / TARGET_SR as f64);
                if !left_b.is_empty() {
                    side_durations.push(left_b.len() as f64 / TARGET_SR as f64);
                }
                actual_dur = dur;
            } else if sides == 2 {
                left_a = left.clone();
                right_a = right.clone();
                left_b = left.clone();
                right_b = right.clone();
                side_durations.push(left_a.len() as f64 / TARGET_SR as f64);
                side_durations.push(left_b.len() as f64 / TARGET_SR as f64);
                actual_dur = dur;
            } else {
                left_a = left;
                right_a = right;
                side_durations.push(dur);
                actual_dur = dur;
            }
        }
    }

    let mut stl_files: Vec<String> = Vec::new();
    let mut tri_counts: Vec<u32> = Vec::new();
    let mut total_points: usize = 0;

    let out_a = format!("{}_sideA.stl", output_prefix);
    match process_side("A", &left_a, &right_a, &out_a, size_inch, rpm, groove_mode, quality, groove_spacing_factor, output_mode, "A") {
        Ok((tris, pts)) => {
            stl_files.push(out_a.clone());
            tri_counts.push(tris);
            total_points += pts;
        }
        Err(err) => return json!({ "ok": false, "error": err }),
    }

    if !left_b.is_empty() || !right_b.is_empty() {
        let out_b = format!("{}_sideB.stl", output_prefix);
        match process_side("B", &left_b, &right_b, &out_b, size_inch, rpm, groove_mode, quality, groove_spacing_factor, output_mode, "B") {
            Ok((tris, pts)) => {
                stl_files.push(out_b.clone());
                tri_counts.push(tris);
                total_points += pts;
            }
            Err(err) => return json!({ "ok": false, "error": err }),
        }
    }

    let report_path = format!("{}_report.json", output_prefix);
    let report = json!({
        "ok": true,
        "report": {
            "record": {
                "size_inch": size_inch,
                "rpm": rpm,
                "groove_mode": groove_mode,
                "sides": sides,
                "split_mode": split_mode,
            },
            "groove": {
                "spacing_factor": groove_spacing_factor,
                "total_points": total_points,
            },
            "audio": {
                "max_duration_s": max_dur,
                "actual_duration_s": actual_dur,
                "side_durations_s": side_durations,
                "multi_files_mode": multi_files_mode,
                "silence_between_s": silence_between,
                "silence_between_a_s": silence_between_a,
                "silence_between_b_s": silence_between_b,
            },
            "stl_output": {
                "files": stl_files,
                "triangle_counts": tri_counts,
                "total_triangles": tri_counts.iter().sum::<u32>(),
            }
        }
    });

    if let Ok(mut f) = File::create(&report_path) {
        let _ = write!(f, "{}", serde_json::to_string_pretty(&report["report"]).unwrap_or_default());
    }

    report
}

fn calculate_groove_handler(req: &Value) -> Value {
    let size_inch = req.get("size_inch").and_then(|v| v.as_u64()).unwrap_or(12) as u8;
    let rpm = req.get("rpm").and_then(|v| v.as_u64()).unwrap_or(33) as u8;
    let groove_mode = req.get("groove_mode").and_then(|v| v.as_str()).unwrap_or("stereo");
    let quality = req.get("quality").and_then(|v| v.as_str()).unwrap_or("high");
    let sample_rate = req.get("sample_rate").and_then(|v| v.as_u64()).unwrap_or(44100) as u32;
    let side = req.get("side").and_then(|v| v.as_str()).unwrap_or("A");
    let spacing_factor = req.get("groove_spacing_factor").and_then(|v| v.as_f64()).unwrap_or(1.0);

    let left_samples = req.get("left_samples")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect::<Vec<_>>())
        .unwrap_or_default();
    
    let right_samples = req.get("right_samples")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect::<Vec<_>>())
        .unwrap_or_default();

    if left_samples.is_empty() || right_samples.is_empty() {
        return json!({ "ok": false, "error": "Audio samples required" });
    }

    let calculator = groove::GrooveCalculator::new(
        size_inch,
        rpm,
        groove_mode,
        quality,
        spacing_factor,
        side,
    );
    let points = calculator.generate(&left_samples, &right_samples, sample_rate);
    
    let point_data: Vec<_> = points.iter().map(|p| {
        json!({
            "x": p.x, "y": p.y, "z_floor": p.z_floor,
            "x_left": p.x_left, "y_left": p.y_left,
            "x_right": p.x_right, "y_right": p.y_right,
        })
    }).collect();
    
    json!({
        "ok": true,
        "points": point_data,
        "count": point_data.len(),
    })
}

fn resample_audio_handler(req: &Value) -> Value {
    let from_rate = req.get("from_rate").and_then(|v| v.as_u64()).unwrap_or(44100) as u32;
    let to_rate = req.get("to_rate").and_then(|v| v.as_u64()).unwrap_or(44100) as u32;
    
    let samples = req.get("samples")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect::<Vec<_>>())
        .unwrap_or_default();
    
    if samples.is_empty() {
        return json!({ "ok": false, "error": "No audio samples provided" });
    }
    
    let resampled = resample::resample(&samples, from_rate, to_rate);
    
    json!({
        "ok": true,
        "samples": resampled.iter().map(|&s| s as f64).collect::<Vec<_>>(),
        "new_length": resampled.len(),
    })
}

fn apply_riaa_handler(req: &Value) -> Value {
    let sample_rate = req.get("sample_rate").and_then(|v| v.as_u64()).unwrap_or(44100) as u32;
    
    let samples = req.get("samples")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect::<Vec<_>>())
        .unwrap_or_default();
    
    if samples.is_empty() {
        return json!({ "ok": false, "error": "No audio samples provided" });
    }
    
    let mut filtered = samples.clone();
    let mut filter = riaa::RiaaPreemphasis::new(sample_rate);
    filter.process(&mut filtered);
    
    json!({
        "ok": true,
        "samples": filtered.iter().map(|&s| s as f64).collect::<Vec<_>>(),
    })
}
