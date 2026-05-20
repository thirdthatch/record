// riaa.rs
// -------
// RIAA recording equalization pre-emphasis filter.
//
// Transfer function (analog):
//   H(s) = (1 + s·T2) / ((1 + s·T1)(1 + s·T3))
//
// Implemented as two cascaded first-order IIR sections via bilinear transform.
// Boosts highs above ~2 kHz, cuts lows below ~500 Hz — standard for disc cutting.

use crate::specs::{RIAA_T1_US, RIAA_T2_US, RIAA_T3_US};

/// Two-section RIAA pre-emphasis IIR filter (bilinear transform).
pub struct RiaaPreemphasis {
    // Section 1 coefficients (zero at T2, pole at T1)
    b0_1: f64, b1_1: f64, a1_1: f64,
    // Section 2 coefficients (pole at T3 — high roll-off)
    b0_2: f64, b1_2: f64, a1_2: f64,
    // Filter state
    z1_1: f64,
    z1_2: f64,
}

impl RiaaPreemphasis {
    pub fn new(sample_rate: u32) -> Self {
        let sr = sample_rate as f64;
        let t  = 1.0 / sr;

        // Analog corner frequencies (rad/s)
        let w1 = 1.0 / (RIAA_T1_US * 1e-6);
        let w2 = 1.0 / (RIAA_T2_US * 1e-6);
        let w3 = 1.0 / (RIAA_T3_US * 1e-6);

        // Bilinear-transform pre-warp
        let w1d = 2.0 * sr * (w1 * t / 2.0).tan();
        let w2d = 2.0 * sr * (w2 * t / 2.0).tan();
        let w3d = 2.0 * sr * (w3 * t / 2.0).tan();

        // Section 1: zero at w2, pole at w1
        let k1 = w1d / (2.0 * sr);
        let k2 = w2d / (2.0 * sr);
        let b0_1 = (1.0 + k2) / (1.0 + k1);
        let b1_1 = (k2 - 1.0) / (1.0 + k1);
        let a1_1 = (k1 - 1.0) / (1.0 + k1);

        // Section 2: pole at w3 (one-pole low-pass for T3 roll-off)
        let k3 = w3d / (2.0 * sr);
        let b0_2 =  1.0 / (1.0 + k3);
        let b1_2 =  1.0 / (1.0 + k3);
        let a1_2 = (k3 - 1.0) / (1.0 + k3);

        RiaaPreemphasis { b0_1, b1_1, a1_1, b0_2, b1_2, a1_2, z1_1: 0.0, z1_2: 0.0 }
    }

    #[inline]
    fn process_sample(&mut self, x: f64) -> f64 {
        // Section 1
        let y1   = self.b0_1 * x + self.z1_1;
        self.z1_1 = self.b1_1 * x - self.a1_1 * y1;
        // Section 2
        let y2   = self.b0_2 * y1 + self.z1_2;
        self.z1_2 = self.b1_2 * y1 - self.a1_2 * y2;
        y2
    }

    /// Process a full slice of samples in place (single channel).
    pub fn process(&mut self, samples: &mut [f32]) {
        self.z1_1 = 0.0;
        self.z1_2 = 0.0;
        for s in samples.iter_mut() {
            *s = self.process_sample(*s as f64) as f32;
        }
    }
}

/// Apply RIAA pre-emphasis to both channels.
pub fn apply_riaa(left: &mut Vec<f32>, right: &mut Vec<f32>, sample_rate: u32) {
    let mut f = RiaaPreemphasis::new(sample_rate);
    f.process(left);
    f.z1_1 = 0.0; f.z1_2 = 0.0;
    f.process(right);
}

// ── Normalisation & soft-knee peak limiter ─────────────────────────────────

/// Normalise stereo signal to `target_peak` amplitude with soft-knee limiting at 0.95.
pub fn normalise_and_limit(left: &mut Vec<f32>, right: &mut Vec<f32>, target_peak: f32) {
    let peak = left.iter().chain(right.iter())
        .map(|x| x.abs())
        .fold(0.0_f32, f32::max);

    if peak < 1e-9 { return; }
    let scale = target_peak / peak;

    let limiter = |v: f32| -> f32 {
        let v = v * scale;
        if v > 0.95 {
            0.95 + 0.05 * ((v - 0.95) / 0.05).tanh()
        } else if v < -0.95 {
            -0.95 - 0.05 * ((-v - 0.95) / 0.05).tanh()
        } else {
            v
        }
    };
    for s in left.iter_mut()  { *s = limiter(*s); }
    for s in right.iter_mut() { *s = limiter(*s); }
}
