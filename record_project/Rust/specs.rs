// specs.rs
// --------
// Vinyl record physical specifications.
// Mirrors record_specs.py exactly so Python and Rust produce identical geometry.

/// Per-size disc geometry (all values in millimetres)
#[derive(Clone, Copy, Debug)]
pub struct DiscSpec {
    pub outer_r:        f64,
    pub groove_outer_r: f64,
    pub groove_inner_r: f64,
    pub label_r:        f64,
    pub center_hole_r:  f64,
    pub thickness:      f64,
}

pub fn disc_spec(size_inch: u8) -> DiscSpec {
    match size_inch {
        7  => DiscSpec { outer_r: 87.0,  groove_outer_r: 82.5,  groove_inner_r: 35.0,
                         label_r: 33.5,  center_hole_r: 3.75, thickness: 1.8 },
        10 => DiscSpec { outer_r: 127.0, groove_outer_r: 122.5, groove_inner_r: 38.0,
                         label_r: 36.0,  center_hole_r: 3.75, thickness: 1.8 },
        12 => DiscSpec { outer_r: 152.4, groove_outer_r: 146.0, groove_inner_r: 60.0,
                         label_r: 58.0,  center_hole_r: 3.75, thickness: 2.0 },
        _  => panic!("Unknown disc size: {}", size_inch),
    }
}

/// Per-RPM groove geometry (all values in millimetres / degrees)
#[derive(Clone, Copy, Debug)]
pub struct GrooveSpec {
    pub groove_width:   f64,   // V-groove opening width at surface
    pub groove_depth:   f64,   // V-groove depth from surface
    pub groove_spacing: f64,   // Land width between adjacent grooves
    pub cutting_angle:  f64,   // V-groove half-angle (IEC 45.0°)
}

pub fn groove_spec(rpm: u8) -> GrooveSpec {
    match rpm {
        33 => GrooveSpec { groove_width: 0.30, groove_depth: 0.20, groove_spacing: 0.20, cutting_angle: 45.0 },
        45 => GrooveSpec { groove_width: 0.55, groove_depth: 0.28, groove_spacing: 0.28, cutting_angle: 45.0 },
        78 => GrooveSpec { groove_width: 0.65, groove_depth: 0.35, groove_spacing: 0.25, cutting_angle: 45.0 },
        _  => panic!("Unknown RPM: {}", rpm),
    }
}

// ── RIAA time constants (µs) ───────────────────────────────────────────────
pub const RIAA_T1_US: f64 = 3180.0;   // 50.05 Hz pole
pub const RIAA_T2_US: f64 =  318.0;   // 500.5 Hz zero
pub const RIAA_T3_US: f64 =   75.0;   // 2122  Hz pole

// ── IEC 45/45 stereo scale ─────────────────────────────────────────────────
pub const STEREO_45_SCALE: f64 = std::f64::consts::FRAC_1_SQRT_2; // 1/√2

// ── Groove encoding ────────────────────────────────────────────────────────
pub const MAX_DISPLACEMENT_FRACTION: f64 = 0.80;

/// Points-per-revolution lookup for each quality level
pub fn pts_per_rev(quality: &str) -> u32 {
    match quality {
        "preview" =>   360,
        "draft"   =>  1800,
        "high"    =>  3600,
        "full"    =>  7200,
        "max"     => 18000,
        _         =>  3600,  // default: high
    }
}

/// Maximum recordable duration statistics
pub struct DurStats {
    pub duration_s:    f64,
    pub turns:         f64,
    pub groove_len_mm: f64,
    pub pitch_mm:      f64,
    pub v_mean_mm_s:   f64,
}

pub fn calc_max_duration(size_inch: u8, rpm: u8, groove_spacing_factor: f64) -> DurStats {
    let spec  = disc_spec(size_inch);
    let g     = groove_spec(rpm);
    let pitch = g.groove_width + g.groove_spacing * groove_spacing_factor;
    let turns = (spec.groove_outer_r - spec.groove_inner_r) / pitch;
    let r_mean       = (spec.groove_outer_r + spec.groove_inner_r) / 2.0;
    let groove_len   = 2.0 * std::f64::consts::PI * r_mean * turns;
    let v_mean_mm_s  = 2.0 * std::f64::consts::PI * r_mean * rpm as f64 / 60.0;
    let duration_s   = groove_len / v_mean_mm_s;
    DurStats { duration_s, turns, groove_len_mm: groove_len, pitch_mm: pitch, v_mean_mm_s }
}
