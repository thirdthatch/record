// stl.rs
// ------
// Binary STL generation for 3D-printable vinyl records.
//
// Two output modes:
//   Single    – collect all triangles, write once (fast for small discs)
//   Streaming – write each triangle immediately (O(1) RAM for large discs)
//
// Disc geometry:
//   • Bottom face (ring, normal -Z)
//   • Top    face (ring, normal +Z)
//   • Outer cylindrical wall
//   • Inner spindle-hole wall
//   • Groove spiral (V-groove per consecutive GroovePoint pair)

use std::io::{BufWriter, Write, Seek, SeekFrom};
use std::fs::File;

use crate::groove::GroovePoint;
use crate::specs::{disc_spec, groove_spec};

// ── Vector math helpers ────────────────────────────────────────────────────

type Vec3 = [f64; 3];

#[inline]
fn sub(a: Vec3, b: Vec3) -> Vec3 { [a[0]-b[0], a[1]-b[1], a[2]-b[2]] }

#[inline]
fn cross(a: Vec3, b: Vec3) -> Vec3 {
    [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
}

#[inline]
fn normalise(v: Vec3) -> Vec3 {
    let l = (v[0]*v[0] + v[1]*v[1] + v[2]*v[2]).sqrt();
    if l < 1e-12 { [0.0, 0.0, 1.0] } else { [v[0]/l, v[1]/l, v[2]/l] }
}

/// Pack one 50-byte STL triangle record into `buf`.
/// Returns number of bytes written (always 50).
fn pack_tri(buf: &mut [u8; 50], n: Vec3, v0: Vec3, v1: Vec3, v2: Vec3) {
    let mut off = 0;
    for val in n.iter().chain(v0.iter()).chain(v1.iter()).chain(v2.iter()) {
        let bytes = (*val as f32).to_le_bytes();
        buf[off..off+4].copy_from_slice(&bytes);
        off += 4;
    }
    // attribute byte count = 0
    buf[48] = 0;
    buf[49] = 0;
}

fn make_tri_buf(buf: &mut [u8; 50], v0: Vec3, v1: Vec3, v2: Vec3) {
    let n = normalise(cross(sub(v1, v0), sub(v2, v0)));
    pack_tri(buf, n, v0, v1, v2);
}

// ── Geometry generators ────────────────────────────────────────────────────

const N_DISC_SEGS: usize = 360;
const TAU: f64 = std::f64::consts::TAU;

/// Iterate over ring-face triangles (flat annular face at height `z`).
/// `flip` = true → normal faces downward (bottom face).
fn ring_face_tris(
    r_outer: f64, r_inner: f64, z: f64, flip: bool,
    mut cb: impl FnMut(Vec3, Vec3, Vec3),
) {
    for i in 0..N_DISC_SEGS {
        let a0 = TAU * i       as f64 / N_DISC_SEGS as f64;
        let a1 = TAU * (i + 1) as f64 / N_DISC_SEGS as f64;
        let o0 = [r_outer * a0.cos(), r_outer * a0.sin(), z];
        let o1 = [r_outer * a1.cos(), r_outer * a1.sin(), z];
        let i0 = [r_inner * a0.cos(), r_inner * a0.sin(), z];
        let i1 = [r_inner * a1.cos(), r_inner * a1.sin(), z];
        if flip {
            cb(o0, o1, i0);
            cb(i0, o1, i1);
        } else {
            cb(o0, i0, o1);
            cb(i0, i1, o1);
        }
    }
}

/// Cylindrical wall at radius `r`.
/// `outward` = true → outer wall (normal away from axis).
fn cylinder_wall_tris(
    r: f64, z_bot: f64, z_top: f64, outward: bool,
    mut cb: impl FnMut(Vec3, Vec3, Vec3),
) {
    for i in 0..N_DISC_SEGS {
        let a0 = TAU * i       as f64 / N_DISC_SEGS as f64;
        let a1 = TAU * (i + 1) as f64 / N_DISC_SEGS as f64;
        let b0 = [r * a0.cos(), r * a0.sin(), z_bot];
        let b1 = [r * a1.cos(), r * a1.sin(), z_bot];
        let t0 = [r * a0.cos(), r * a0.sin(), z_top];
        let t1 = [r * a1.cos(), r * a1.sin(), z_top];
        if outward {
            cb(b0, b1, t0);
            cb(b1, t1, t0);
        } else {
            cb(b0, t0, b1);
            cb(b1, t0, t1);
        }
    }
}

/// V-groove triangles for one segment between consecutive GroovePoints.
/// Emits 4 triangles: 2 for left wall, 2 for right wall.
fn groove_seg_tris(
    p0: &GroovePoint, p1: &GroovePoint, z_surf: f64,
    mut cb: impl FnMut(Vec3, Vec3, Vec3),
) {
    let vl0: Vec3 = [p0.x_left,  p0.y_left,  z_surf];
    let vl1: Vec3 = [p1.x_left,  p1.y_left,  z_surf];
    let vr0: Vec3 = [p0.x_right, p0.y_right, z_surf];
    let vr1: Vec3 = [p1.x_right, p1.y_right, z_surf];
    let fl0: Vec3 = [p0.x, p0.y, p0.z_floor];
    let fl1: Vec3 = [p1.x, p1.y, p1.z_floor];

    // Left wall
    cb(vl0, fl0, vl1);
    cb(fl0, fl1, vl1);
    // Right wall
    cb(fl0, vr0, fl1);
    cb(vr0, vr1, fl1);
}

// ── STL writer ─────────────────────────────────────────────────────────────

pub struct StlConfig {
    pub size_inch:  u8,
    pub rpm:        u8,
    pub z_offset:   f64,   // for stacking two sides
    pub output_path: String,
    pub streaming:  bool,
}

/// Write STL in streaming mode (O(1) RAM).
///
/// Opens the file, writes the header with a placeholder triangle count,
/// streams all triangles, then seeks back and patches the real count.
pub fn write_stl_streaming(
    config: &StlConfig,
    groove_pts: &[GroovePoint],
    progress_cb: impl Fn(u32),  // called with 0..100
) -> std::io::Result<u32> {
    let spec  = disc_spec(config.size_inch);
    let outer_r  = spec.outer_r;
    let hole_r   = spec.center_hole_r;
    let thickness = spec.thickness;
    let z_off    = config.z_offset;
    let z_bot    = z_off;
    let z_top    = z_off + thickness;
    let z_surf   = z_top;  // groove sits on top face

    let file  = File::create(&config.output_path)?;
    let mut w = BufWriter::with_capacity(1 << 20, file);  // 1 MB write buffer

    // Header (80 bytes) + placeholder count (4 bytes)
    w.write_all(&[0u8; 80])?;
    w.write_all(&0u32.to_le_bytes())?;

    let mut count = 0u32;
    let mut buf   = [0u8; 50];

    let total_segs = (groove_pts.len().saturating_sub(1)) as u32;
    let disc_tris  = (N_DISC_SEGS * 4 * 2) as u32;   // 4 surfaces × N_SEGS × 2 tris
    let total_tris = disc_tris + total_segs * 4;

    let mut emit = |v0: Vec3, v1: Vec3, v2: Vec3| -> std::io::Result<()> {
        // Apply z_offset to all vertices
        let v0z = [v0[0], v0[1], v0[2] + z_off];
        let v1z = [v1[0], v1[1], v1[2] + z_off];
        let v2z = [v2[0], v2[1], v2[2] + z_off];
        make_tri_buf(&mut buf, v0z, v1z, v2z);
        w.write_all(&buf)?;
        count += 1;
        Ok(())
    };

    // ── Disc body ─────────────────────────────────────────────────────────
    ring_face_tris(outer_r, hole_r, z_bot, true,  |a,b,c| { emit(a,b,c).unwrap(); });
    ring_face_tris(outer_r, hole_r, z_top, false, |a,b,c| { emit(a,b,c).unwrap(); });
    cylinder_wall_tris(outer_r, z_bot, z_top, true,  |a,b,c| { emit(a,b,c).unwrap(); });
    cylinder_wall_tris(hole_r,  z_bot, z_top, false, |a,b,c| { emit(a,b,c).unwrap(); });

    progress_cb(20);

    // ── Groove spiral ─────────────────────────────────────────────────────
    let report_every = (groove_pts.len() / 20).max(1);
    for i in 0..groove_pts.len().saturating_sub(1) {
        groove_seg_tris(&groove_pts[i], &groove_pts[i+1], z_surf, |a,b,c| {
            emit(a, b, c).unwrap();
        });
        if i % report_every == 0 {
            let pct = 20 + (80 * i / groove_pts.len()) as u32;
            progress_cb(pct);
        }
    }
    progress_cb(100);

    // Flush and patch triangle count
    w.flush()?;
    let mut file = w.into_inner().map_err(|e| e.into_error())?;
    file.seek(SeekFrom::Start(80))?;
    file.write_all(&count.to_le_bytes())?;

    Ok(count)
}

/// Write STL by collecting all triangles first (simpler, uses more RAM).
/// Returns triangle count.
pub fn write_stl_single(
    config: &StlConfig,
    groove_pts: &[GroovePoint],
    progress_cb: impl Fn(u32),
) -> std::io::Result<u32> {
    let spec     = disc_spec(config.size_inch);
    let outer_r  = spec.outer_r;
    let hole_r   = spec.center_hole_r;
    let thickness = spec.thickness;
    let z_off    = config.z_offset;
    let z_bot    = z_off;
    let z_top    = z_off + thickness;
    let z_surf   = z_top;

    // Estimate capacity
    let disc_cap  = N_DISC_SEGS * 4 * 2;
    let grove_cap = groove_pts.len().saturating_sub(1) * 4;
    let mut tris: Vec<[u8; 50]> = Vec::with_capacity(disc_cap + grove_cap);

    let z_off_ref = z_off;
    let mut push = |v0: Vec3, v1: Vec3, v2: Vec3| {
        let v0z = [v0[0], v0[1], v0[2] + z_off_ref];
        let v1z = [v1[0], v1[1], v1[2] + z_off_ref];
        let v2z = [v2[0], v2[1], v2[2] + z_off_ref];
        let mut buf = [0u8; 50];
        make_tri_buf(&mut buf, v0z, v1z, v2z);
        tris.push(buf);
    };

    // Disc body
    ring_face_tris(outer_r, hole_r, z_bot, true,  |a,b,c| push(a,b,c));
    ring_face_tris(outer_r, hole_r, z_top, false, |a,b,c| push(a,b,c));
    cylinder_wall_tris(outer_r, z_bot, z_top, true,  |a,b,c| push(a,b,c));
    cylinder_wall_tris(hole_r,  z_bot, z_top, false, |a,b,c| push(a,b,c));

    progress_cb(20);

    // Groove
    let report_every = (groove_pts.len() / 20).max(1);
    for i in 0..groove_pts.len().saturating_sub(1) {
        groove_seg_tris(&groove_pts[i], &groove_pts[i+1], z_surf, |a,b,c| push(a,b,c));
        if i % report_every == 0 {
            let pct = 20 + (80 * i / groove_pts.len()) as u32;
            progress_cb(pct);
        }
    }
    progress_cb(100);

    // Write
    let count = tris.len() as u32;
    let mut w = BufWriter::new(File::create(&config.output_path)?);
    w.write_all(&[0u8; 80])?;
    w.write_all(&count.to_le_bytes())?;
    for t in &tris {
        w.write_all(t)?;
    }
    w.flush()?;
    Ok(count)
}
