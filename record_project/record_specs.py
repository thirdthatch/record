"""
record_specs.py
---------------
Vinyl record physical specifications and constants.
All measurements in millimeters unless otherwise noted.
"""

# ── Record size specifications ─────────────────────────────────────────────
# Based on IEC 60098 / RIAA standard dimensions
RECORD_SPECS = {
    7: {
        "outer_r":        87.0,    # Outer radius (174mm diameter)
        "groove_outer_r": 82.5,    # Outermost groove radius
        "groove_inner_r": 35.0,    # Innermost groove radius (lead-out start)
        "leadout_r":      33.5,    # Lead-out groove radius
        "label_r":        33.5,    # Label area radius
        "center_hole_r":  3.75,    # Center spindle hole (7.5mm diameter)
        "thickness":      1.8,     # Disc thickness
        "desc":           '7" Single (45rpm standard)',
    },
    10: {
        "outer_r":        127.0,
        "groove_outer_r": 122.5,
        "groove_inner_r": 38.0,
        "leadout_r":      36.0,
        "label_r":        36.0,
        "center_hole_r":  3.75,
        "thickness":      1.8,
        "desc":           '10" Medium (78rpm standard)',
    },
    12: {
        "outer_r":        152.4,
        "groove_outer_r": 146.0,
        "groove_inner_r": 60.0,    # LP standard inner groove
        "leadout_r":      58.0,
        "label_r":        58.0,
        "center_hole_r":  3.75,
        "thickness":      2.0,
        "desc":           '12" LP / 12" Single',
    },
}

# ── RPM → groove geometry ──────────────────────────────────────────────────
# groove_width:   V-groove opening width at the surface (mm)
# groove_depth:   V-groove depth from surface (mm)
# groove_spacing: Land width between adjacent grooves (mm)
# cutting_angle:  V-groove half-angle (degrees, IEC standard = 45°)
RPM_GROOVE = {
    33: {
        "groove_width":   0.30,
        "groove_depth":   0.20,
        "groove_spacing": 0.20,
        "cutting_angle":  45.0,
        "desc":           "LP long-play (33⅓ RPM)",
    },
    45: {
        "groove_width":   0.55,
        "groove_depth":   0.28,
        "groove_spacing": 0.28,
        "cutting_angle":  45.0,
        "desc":           "Single (45 RPM)",
    },
    78: {
        "groove_width":   0.65,
        "groove_depth":   0.35,
        "groove_spacing": 0.25,
        "cutting_angle":  45.0,
        "desc":           "SP (78 RPM)",
    },
}

# ── RIAA equalization curve ────────────────────────────────────────────────
# Time constants (microseconds) per IEC 60098 / RIAA spec
RIAA_T1_US = 3180.0   # 50.05 Hz pole
RIAA_T2_US =  318.0   # 500.5 Hz zero/pole
RIAA_T3_US =   75.0   # 2122  Hz zero

# ── Groove encoding constants ──────────────────────────────────────────────
# IEC 45/45 stereo standard:
#   Left  wall displacement = (L + R) / sqrt(2)  [lateral component]
#   Right wall displacement = (L - R) / sqrt(2)  [vertical component]
STEREO_45_SCALE = 0.7071067811865476  # 1 / sqrt(2)

# Maximum groove wall displacement as fraction of half groove width
# Prevents groove walls from crossing (overcut)
MAX_DISPLACEMENT_FRACTION = 0.80

# Target internal sample rate for groove encoding
# Must satisfy Nyquist for max recorded frequency
# 33rpm: 33/60 rev/s, Nyquist needs 2x max_freq samples/rev
# At 12kHz target max: 12000 / (33/60) = ~21818 samples/rev minimum
# We use 44100 Hz which gives: 44100 / (33/60) ≈ 80,182 samples/rev
GROOVE_SAMPLE_RATE = 44100

# Points per revolution for STL groove path
# At 44100 Hz, 33 RPM: 44100 * 60/33 = 80,182 pts/rev is the maximum
# For balance between fidelity and file size:
#   HIGH quality:  pts_per_rev = 3600   (~10° resolution, good for audible range)
#   FULL quality:  pts_per_rev = 7200   (~0.05° resolution, 6kHz at 33rpm)
#   MAX quality:   pts_per_rev = 18000  (~0.02° resolution, ~15kHz at 33rpm)
GROOVE_PTS_PER_REV = {
    "preview": 360,
    "draft":   1800,
    "high":    3600,
    "full":    7200,
    "max":     18000,
}

# Frequency response limits per quality level (approximate, 33 RPM)
GROOVE_MAX_FREQ_HZ = {
    "preview": 200,
    "draft":   1000,
    "high":    2000,
    "full":    4000,
    "max":     10000,
}

# ── STL output modes ───────────────────────────────────────────────────────
STL_MODE_SINGLE    = "single"    # One file, all geometry in memory then write
STL_MODE_STREAMING = "streaming" # Write triangles directly to file as generated

# ── Printing recommendations ───────────────────────────────────────────────
PRINT_RECOMMENDATIONS = {
    33: {
        "layer_height_mm":  0.05,
        "nozzle_mm":        0.25,
        "speed_mm_s":       20,
        "material":         "Resin (SLA/DLP) or PLA fine",
        "infill_pct":       100,
        "note":             "SLA/DLP strongly recommended. FDM 0.25mm nozzle minimum.",
    },
    45: {
        "layer_height_mm":  0.05,
        "nozzle_mm":        0.25,
        "speed_mm_s":       20,
        "material":         "Resin (SLA/DLP) or PLA fine",
        "infill_pct":       100,
        "note":             "SLA/DLP strongly recommended.",
    },
    78: {
        "layer_height_mm":  0.08,
        "nozzle_mm":        0.30,
        "speed_mm_s":       25,
        "material":         "Resin (SLA/DLP) or PLA",
        "infill_pct":       100,
        "note":             "Wider grooves allow FDM with 0.3mm nozzle.",
    },
}

def calc_max_duration(size_inch: int, rpm: int, groove_spacing_factor: float = 1.0) -> dict:
    """
    Calculate maximum recordable duration and groove statistics
    for a given disc size and RPM.
    
    Args:
        size_inch: Record size in inches (7, 10, or 12)
        rpm: Rotation speed (33, 45, or 78)
        groove_spacing_factor: Multiplier for groove spacing (0.5 to 2.0)
                              < 1.0 : longer duration
                              > 1.0 : shorter duration

    Returns dict with:
        duration_s   : max recordable duration in seconds
        turns        : number of groove turns
        groove_len_mm: total groove length in mm
        pitch_mm     : groove pitch (radial advance per revolution)
        v_mean_mm_s  : mean groove velocity (mm/s)
        spacing_factor: applied groove spacing factor
    """
    spec  = RECORD_SPECS[size_inch]
    groove = RPM_GROOVE[rpm]

    r_out  = spec["groove_outer_r"]
    r_in   = spec["groove_inner_r"]
    
    # Apply groove spacing factor
    adjusted_spacing = groove["groove_spacing"] * groove_spacing_factor
    pitch  = groove["groove_width"] + adjusted_spacing
    turns  = (r_out - r_in) / pitch

    r_mean       = (r_out + r_in) / 2.0
    groove_len   = 2.0 * 3.141592653589793 * r_mean * turns
    v_mean_mm_s  = 2.0 * 3.141592653589793 * r_mean * rpm / 60.0
    duration_s   = groove_len / v_mean_mm_s

    return {
        "duration_s":    duration_s,
        "turns":         turns,
        "groove_len_mm": groove_len,
        "pitch_mm":      pitch,
        "v_mean_mm_s":   v_mean_mm_s,
        "spacing_factor": groove_spacing_factor,
    }
