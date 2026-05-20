"""
reverse_record/record_specs.py
-----------------------------
Minimal vinyl record geometry and RIAA constants used by the reverse
STL-to-audio extractor.
"""

RECORD_SPECS = {
    7: {
        "outer_r":        87.0,
        "groove_outer_r": 82.5,
        "groove_inner_r": 35.0,
        "center_hole_r":  3.75,
        "thickness":      1.8,
    },
    10: {
        "outer_r":        127.0,
        "groove_outer_r": 122.5,
        "groove_inner_r": 38.0,
        "center_hole_r":  3.75,
        "thickness":      1.8,
    },
    12: {
        "outer_r":        152.4,
        "groove_outer_r": 146.0,
        "groove_inner_r": 60.0,
        "center_hole_r":  3.75,
        "thickness":      2.0,
    },
}

RPM_GROOVE = {
    33: {
        "groove_width":   0.30,
        "groove_depth":   0.20,
        "groove_spacing": 0.20,
    },
    45: {
        "groove_width":   0.55,
        "groove_depth":   0.28,
        "groove_spacing": 0.28,
    },
    78: {
        "groove_width":   0.65,
        "groove_depth":   0.35,
        "groove_spacing": 0.25,
    },
}

STEREO_45_SCALE = 0.7071067811865476
MAX_DISPLACEMENT_FRACTION = 0.80
GROOVE_SAMPLE_RATE = 44100
GROOVE_PTS_PER_REV = {
    "preview": 360,
    "draft":   1800,
    "high":    3600,
    "full":    7200,
    "max":     18000,
}

RIAA_T1_US = 3180.0
RIAA_T2_US =  318.0
RIAA_T3_US =   75.0
