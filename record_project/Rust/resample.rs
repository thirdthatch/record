// resample.rs
// -----------
// High-quality audio resampler using a windowed-sinc (Lanczos) kernel.
//
// Lanczos-a=3 gives much better anti-aliasing than linear interpolation,
// which matters when downsampling audio before groove encoding.
//
// For production use Lanczos-3 is a good quality/speed compromise:
//   - Lobe radius: a = 3 samples
//   - Passband ripple < 0.1 dB
//   - Stopband attenuation ~-40 dB

const LANCZOS_A: i64 = 3;   // kernel half-width in source samples

/// Lanczos kernel value at `x` with window parameter `a`.
#[inline]
fn lanczos(x: f64, a: f64) -> f64 {
    if x.abs() < 1e-9 {
        return 1.0;
    }
    if x.abs() >= a {
        return 0.0;
    }
    let pi_x = std::f64::consts::PI * x;
    (pi_x.sin() / pi_x) * ((pi_x / a).sin() / (pi_x / a))
}

/// Resample `src` from `from_rate` Hz to `to_rate` Hz using Lanczos-3.
/// Returns a new Vec<f32> at the target sample rate.
pub fn resample(src: &[f32], from_rate: u32, to_rate: u32) -> Vec<f32> {
    if from_rate == to_rate {
        return src.to_vec();
    }
    let ratio  = from_rate as f64 / to_rate as f64;   // source samples per output sample
    let new_len = ((src.len() as f64) / ratio).ceil() as usize;
    let n = src.len() as i64;
    let a = LANCZOS_A as f64;

    let mut out = Vec::with_capacity(new_len);

    for i in 0..new_len {
        let src_pos = i as f64 * ratio;
        let center  = src_pos.floor() as i64;

        let mut acc    = 0.0_f64;
        let mut weight = 0.0_f64;

        for k in (center - LANCZOS_A + 1)..=(center + LANCZOS_A) {
            if k < 0 || k >= n { continue; }
            let w = lanczos(src_pos - k as f64, a);
            acc    += src[k as usize] as f64 * w;
            weight += w;
        }

        let v = if weight.abs() > 1e-12 { (acc / weight) as f32 } else { 0.0 };
        out.push(v.clamp(-1.0, 1.0));
    }
    out
}
