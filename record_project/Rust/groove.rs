// groove.rs
// ---------
// IEC 60098 / RIAA 45/45 stereo groove path calculation.
//
// Modulation equations (IEC 45/45):
//   lateral  = (L + R) / √2   — horizontal movement of groove centre
//   vertical = (L - R) / √2   — vertical movement of groove centre
//
// Each GroovePoint stores the groove centre (x, y, z_floor) and the
// surface-level wall edge positions (x_left/y_left, x_right/y_right).
// The STL writer uses these directly to build V-groove triangles.

use crate::specs::{
    disc_spec, groove_spec, calc_max_duration,
    STEREO_45_SCALE, MAX_DISPLACEMENT_FRACTION, pts_per_rev,
};
use rayon::prelude::*;

// ── Data type ──────────────────────────────────────────────────────────────

/// A single point along the groove spiral (all coordinates in mm).
#[derive(Clone, Debug)]
pub struct GroovePoint {
    /// Groove centre, x/y on disc top surface
    pub x:       f64,
    pub y:       f64,
    /// Z height of groove floor (mm from disc bottom)
    pub z_floor: f64,
    /// Surface-level left-wall edge (inner side of groove)
    pub x_left:  f64,
    pub y_left:  f64,
    /// Surface-level right-wall edge (outer side of groove)
    pub x_right: f64,
    pub y_right: f64,
}

// ── Calculator ─────────────────────────────────────────────────────────────

pub struct GrooveCalculator {
    pub size_inch:   u8,
    pub rpm:         u8,
    pub groove_mode: String,    // "mono" | "stereo"
    pub quality:     String,    // "preview" | "draft" | "high" | "full" | "max"
    pub side:        String,    // "A" | "B"

    // Derived geometry
    r_out:    f64,
    r_in:     f64,
    thickness: f64,
    pitch:    f64,
    depth:    f64,
    half_w:   f64,
    max_disp: f64,
    turns:    f64,
}

impl GrooveCalculator {
    pub fn new(
        size_inch: u8,
        rpm: u8,
        groove_mode: &str,
        quality: &str,
        groove_spacing_factor: f64,
        side: &str,
    ) -> Self {
        let spec  = disc_spec(size_inch);
        let g     = groove_spec(rpm);
        let stats = calc_max_duration(size_inch, rpm, groove_spacing_factor);

        let pitch    = g.groove_width + g.groove_spacing * groove_spacing_factor;
        let half_w   = g.groove_width / 2.0;
        let max_disp = half_w * MAX_DISPLACEMENT_FRACTION;

        GrooveCalculator {
            size_inch,
            rpm,
            groove_mode: groove_mode.to_string(),
            quality:     quality.to_string(),
            side:        side.to_string(),
            r_out:    spec.groove_outer_r,
            r_in:     spec.groove_inner_r,
            thickness: spec.thickness,
            pitch,
            depth:    g.groove_depth,
            half_w,
            max_disp,
            turns:    stats.turns,
        }
    }

    /// Generate the full groove path from stereo audio samples.
    ///
    /// Uses Rayon to parallelise the per-point computation across CPU cores.
    /// Audio lookups are read-only slice accesses — no locking needed.
    ///
    /// # Arguments
    /// * `left`        – left channel samples, normalised to [-1, 1]
    /// * `right`       – right channel samples (same length as left)
    /// * `sample_rate` – samples/second of the input audio
    pub fn generate(
        &self,
        left:        &[f32],
        right:       &[f32],
        sample_rate: u32,
    ) -> Vec<GroovePoint> {
        let n_samples    = left.len().min(right.len());
        let samp_per_rev = sample_rate as f64 * 60.0 / self.rpm as f64;
        let total_revs   = (n_samples as f64 / samp_per_rev).min(self.turns);
        let ppr          = pts_per_rev(&self.quality) as usize;
        let total_pts    = (total_revs * ppr as f64) as usize;
        let is_stereo    = self.groove_mode == "stereo";
        let is_side_b    = self.side == "B";

        // Optionally reverse B-side audio so it plays outer→inner
        let (left, right) = if is_side_b {
            (left.iter().rev().cloned().collect::<Vec<_>>(),
             right.iter().rev().cloned().collect::<Vec<_>>())
        } else {
            (left.to_vec(), right.to_vec())
        };

        // Pre-capture values so the parallel closure is Send + Sync
        let n_samples = left.len().min(right.len());
        let r_out    = self.r_out;
        let pitch    = self.pitch;
        let depth    = self.depth;
        let half_w   = self.half_w;
        let max_disp = self.max_disp;
        let thickness = self.thickness;
        let pi2      = 2.0 * std::f64::consts::PI;

        (0..total_pts)
            .into_par_iter()
            .map(|i| {
                let frac     = i as f64 / total_pts as f64;
                let angle    = frac * total_revs * pi2;        // CCW in math coords
                let r_center = r_out - frac * total_revs * pitch;

                // Audio sample index
                let si = ((frac * total_revs * samp_per_rev) as usize).min(n_samples - 1);
                let l  = left[si]  as f64;
                let r  = right[si] as f64;

                // ── IEC 45/45 modulation ────────────────────────────────
                let (d_lat, d_vert) = if is_stereo {
                    (
                        ((l + r) * STEREO_45_SCALE * max_disp)
                            .clamp(-max_disp, max_disp),
                        ((l - r) * STEREO_45_SCALE * depth * 0.35)
                            .clamp(-depth * 0.4, depth * 0.4),
                    )
                } else {
                    (
                        ((l + r) / 2.0 * max_disp)
                            .clamp(-max_disp, max_disp),
                        0.0,
                    )
                };

                // ── Groove centre position ──────────────────────────────
                // Negate angle → clockwise spiral (standard LP direction)
                let cos_a = (-angle).cos();
                let sin_a = (-angle).sin();

                // Radial normal at this angle
                let nx = cos_a;
                let ny = sin_a;

                let cx = r_center * cos_a + d_lat * nx;
                let cy = r_center * sin_a + d_lat * ny;

                // ── Z positions ─────────────────────────────────────────
                let z_floor = (thickness - depth + d_vert)
                    .clamp(0.05, thickness - 0.05);

                // ── Wall edge positions (surface level) ─────────────────
                let x_left  = cx - nx * half_w;
                let y_left  = cy - ny * half_w;
                let x_right = cx + nx * half_w;
                let y_right = cy + ny * half_w;

                GroovePoint { x: cx, y: cy, z_floor, x_left, y_left, x_right, y_right }
            })
            .collect()
    }

    pub fn total_points(&self, n_samples: usize, sample_rate: u32) -> usize {
        let samp_per_rev = sample_rate as f64 * 60.0 / self.rpm as f64;
        let total_revs   = (n_samples as f64 / samp_per_rev).min(self.turns);
        let ppr          = pts_per_rev(&self.quality) as f64;
        (total_revs * ppr) as usize
    }
}
