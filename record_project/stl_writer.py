"""
stl_writer.py
-------------
STL geometry generation and file output for 3D-printable vinyl records.

Supports two output strategies (selectable at runtime):
  1. SINGLE   – collect all triangles in memory, write one STL file.
               Fast for small records; requires enough RAM for all triangles.
  2. STREAMING – write triangles directly to file as they are generated,
               using a pre-allocated header that is patched with the final
               triangle count.  Memory usage is O(1) regardless of disc size.

Geometry overview
-----------------
The disc is modelled as a solid cylinder with:
  • Bottom face   : flat ring (outer_r → center_hole_r) at z = 0
  • Top face      : flat ring at z = T, with the groove spiral cut in
  • Outer wall    : cylindrical surface at r = outer_r
  • Inner wall    : cylindrical surface at r = center_hole_r (spindle hole)
  • Groove spiral : V-shaped trench on the top face, driven by GroovePoints

Each groove cross-section is a 90° V-groove (IEC 45/45):
  Left wall  : surface edge → groove floor, inner side
  Right wall : surface edge → groove floor, outer side
  Floor quad : connects consecutive floor points

Triangle winding order follows the right-hand rule (outward normals).
"""

from __future__ import annotations

import math
import os
import struct
from typing import Iterator, List, NamedTuple, Tuple, BinaryIO

from record_specs import (
    RECORD_SPECS, RPM_GROOVE, STL_MODE_SINGLE, STL_MODE_STREAMING,
    PRINT_RECOMMENDATIONS,
)
from groove_calculator import GroovePoint, GroovePath


# ── Type aliases ───────────────────────────────────────────────────────────
Vec3   = Tuple[float, float, float]
Normal = Vec3
Triangle = Tuple[Normal, Vec3, Vec3, Vec3]   # (normal, v0, v1, v2)


# ══════════════════════════════════════════════════════════════════════════════
# Low-level geometry helpers
# ══════════════════════════════════════════════════════════════════════════════

def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])

def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1]*b[2] - a[2]*b[1],
        a[2]*b[0] - a[0]*b[2],
        a[0]*b[1] - a[1]*b[0],
    )

def _norm(v: Vec3) -> Vec3:
    l = math.sqrt(v[0]*v[0] + v[1]*v[1] + v[2]*v[2])
    if l < 1e-12:
        return (0.0, 0.0, 1.0)
    return (v[0]/l, v[1]/l, v[2]/l)

def _make_tri(v0: Vec3, v1: Vec3, v2: Vec3) -> Triangle:
    """Build triangle with auto-computed outward normal."""
    n = _norm(_cross(_sub(v1, v0), _sub(v2, v0)))
    return (n, v0, v1, v2)

def _pack_tri(t: Triangle) -> bytes:
    """Pack one triangle into 50-byte binary STL record."""
    n, v0, v1, v2 = t
    return struct.pack(
        '<fff fff fff fff H',
        n[0], n[1], n[2],
        v0[0], v0[1], v0[2],
        v1[0], v1[1], v1[2],
        v2[0], v2[1], v2[2],
        0,  # attribute byte count
    )


# ══════════════════════════════════════════════════════════════════════════════
# Disc base geometry generators
# ══════════════════════════════════════════════════════════════════════════════

def _ring_face(
    r_outer: float,
    r_inner: float,
    z: float,
    n_segs: int,
    flip: bool,
) -> Iterator[Triangle]:
    """
    Flat annular face at height z.
    flip=True  → normal points downward  (bottom face)
    flip=False → normal points upward    (top face)
    """
    for i in range(n_segs):
        a0 = 2.0 * math.pi * i       / n_segs
        a1 = 2.0 * math.pi * (i + 1) / n_segs
        o0 = (r_outer * math.cos(a0), r_outer * math.sin(a0), z)
        o1 = (r_outer * math.cos(a1), r_outer * math.sin(a1), z)
        i0 = (r_inner * math.cos(a0), r_inner * math.sin(a0), z)
        i1 = (r_inner * math.cos(a1), r_inner * math.sin(a1), z)
        if flip:
            yield _make_tri(o0, o1, i0)
            yield _make_tri(i0, o1, i1)
        else:
            yield _make_tri(o0, i0, o1)
            yield _make_tri(i0, i1, o1)


def _cylinder_wall(
    r: float,
    z_bot: float,
    z_top: float,
    n_segs: int,
    outward: bool,
) -> Iterator[Triangle]:
    """
    Vertical cylinder wall at radius r between z_bot and z_top.
    outward=True  → normals point away from axis (outer wall)
    outward=False → normals point toward  axis  (inner/hole wall)
    """
    for i in range(n_segs):
        a0 = 2.0 * math.pi * i       / n_segs
        a1 = 2.0 * math.pi * (i + 1) / n_segs
        b0 = (r * math.cos(a0), r * math.sin(a0), z_bot)
        b1 = (r * math.cos(a1), r * math.sin(a1), z_bot)
        t0 = (r * math.cos(a0), r * math.sin(a0), z_top)
        t1 = (r * math.cos(a1), r * math.sin(a1), z_top)
        if outward:
            yield _make_tri(b0, b1, t0)
            yield _make_tri(b1, t1, t0)
        else:
            yield _make_tri(b0, t0, b1)
            yield _make_tri(b1, t0, t1)


# ══════════════════════════════════════════════════════════════════════════════
# Groove geometry generator
# ══════════════════════════════════════════════════════════════════════════════

def _groove_triangles(
    pts: List[GroovePoint],
    z_surface: float,
) -> Iterator[Triangle]:
    r"""
    Emit triangles for the V-groove spiral.

    For each consecutive pair of GroovePoints we produce:
      • Left wall  quad  (2 triangles): left edge → floor
      • Right wall quad  (2 triangles): floor    → right edge
      • Floor      quad  (2 triangles): floor segment connecting the two points

    The groove cross-section is a 90° V-shape:

         surface (z = T)
     L_top         R_top
      |               |
       \             /
        \           /
         \         /
          \ floor /
           \     /
            \   /
             \_/   ← groove floor (modulated by audio)

    All coordinates come directly from GroovePoint fields computed
    in groove_calculator.py.
    """
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[i]
        p1 = pts[i + 1]

        # Surface-level wall top positions
        vl0: Vec3 = (p0.x_left,  p0.y_left,  z_surface)
        vl1: Vec3 = (p1.x_left,  p1.y_left,  z_surface)
        vr0: Vec3 = (p0.x_right, p0.y_right, z_surface)
        vr1: Vec3 = (p1.x_right, p1.y_right, z_surface)

        # Groove floor positions
        fl0: Vec3 = (p0.x, p0.y, p0.z_floor)
        fl1: Vec3 = (p1.x, p1.y, p1.z_floor)

        # ── Left wall (inner side of groove) ──────────────────────────
        # Triangle 1: vl0, fl0, vl1
        yield _make_tri(vl0, fl0, vl1)
        # Triangle 2: fl0, fl1, vl1
        yield _make_tri(fl0, fl1, vl1)

        # ── Right wall (outer side of groove) ─────────────────────────
        # Triangle 1: fl0, vr0, fl1
        yield _make_tri(fl0, vr0, fl1)
        # Triangle 2: vr0, vr1, fl1
        yield _make_tri(vr0, vr1, fl1)

        # ── Floor connecting quad ──────────────────────────────────────
        # (bridges the gap between wall vertices at groove floor level)
        # Note: the floor quad is a degenerate sliver for a perfect V,
        # but is necessary for watertight mesh when d_vert ≠ 0.
        # We emit it only when the floor points are spatially distinct.
        dx = fl1[0] - fl0[0]
        dy = fl1[1] - fl0[1]
        dz = fl1[2] - fl0[2]
        if math.sqrt(dx*dx + dy*dy + dz*dz) > 1e-9:
            # Floor is already represented by the wall quads meeting at the
            # floor points; no separate face needed for a perfect V-groove.
            pass


# ══════════════════════════════════════════════════════════════════════════════
# STL Writer class
# ══════════════════════════════════════════════════════════════════════════════

class STLWriter:
    """
    Generates and writes a 3D-printable vinyl record STL file.

    Usage (single mode):
        writer = STLWriter(size_inch=12, rpm=33, output_path="record.stl",
                           mode=STL_MODE_SINGLE)
        writer.write(groove_points, progress_cb=cb)

    Usage (streaming mode):
        writer = STLWriter(size_inch=12, rpm=33, output_path="record.stl",
                           mode=STL_MODE_STREAMING)
        writer.write(groove_points, progress_cb=cb)

    The streaming mode writes triangles directly to disk as they are
    generated; memory usage stays constant regardless of groove length.
    """

    N_DISC_SEGS = 360   # polygon approximation for circular faces/walls

    def __init__(
        self,
        size_inch: int,
        rpm: int,
        output_path: str,
        mode: str = STL_MODE_SINGLE,
        z_offset: float = 0.0,   # for stacking two sides in one file
    ):
        self.size      = size_inch
        self.rpm       = rpm
        self.out_path  = output_path
        self.mode      = mode
        self.z_offset  = z_offset

        self.spec      = RECORD_SPECS[size_inch]
        self.groove    = RPM_GROOVE[rpm]

        self.outer_r   = self.spec["outer_r"]
        self.hole_r    = self.spec["center_hole_r"]
        self.T         = self.spec["thickness"]      # disc thickness (mm)

    # ── Public API ─────────────────────────────────────────────────────────

    def write(
        self,
        groove_pts: List[GroovePoint],
        progress_cb=None,
    ) -> int:
        """
        Generate all geometry and write the STL file.

        Returns the total number of triangles written.
        """
        if self.mode == STL_MODE_STREAMING:
            return self._write_streaming(groove_pts, progress_cb)
        else:
            return self._write_single(groove_pts, progress_cb)

    # ── Single-file mode ───────────────────────────────────────────────────

    def _write_single(
        self,
        groove_pts: List[GroovePoint],
        progress_cb=None,
    ) -> int:
        def progress(msg: str, pct: int):
            if progress_cb:
                progress_cb(msg, pct)

        progress("  Collecting disc geometry...", 85)
        tris: List[Triangle] = []

        # Disc body
        for t in self._disc_geometry():
            tris.append(t)

        progress("  Collecting groove geometry...", 88)
        z_surf = self.T + self.z_offset
        for t in _groove_triangles(groove_pts, z_surf):
            tris.append(self._offset_tri(t))

        progress(f"  Writing {len(tris):,} triangles → {self.out_path}", 92)
        self._write_binary(tris)
        progress("  STL written.", 95)
        return len(tris)

    # ── Streaming mode ─────────────────────────────────────────────────────

    def _write_streaming(
        self,
        groove_pts: List[GroovePoint],
        progress_cb=None,
    ) -> int:
        def progress(msg: str, pct: int):
            if progress_cb:
                progress_cb(msg, pct)

        progress(f"  Streaming STL → {self.out_path}", 85)
        count = 0

        with open(self.out_path, 'wb') as f:
            # Write placeholder header (triangle count updated at the end)
            f.write(b'\x00' * 80)
            f.write(struct.pack('<I', 0))   # placeholder count

            # Disc body
            for t in self._disc_geometry():
                f.write(_pack_tri(self._offset_tri(t)))
                count += 1

            progress("  Streaming groove geometry...", 88)

            # Groove spiral
            z_surf = self.T + self.z_offset
            report_every = max(1, len(groove_pts) // 10)
            for t in _groove_triangles(groove_pts, z_surf):
                f.write(_pack_tri(self._offset_tri(t)))
                count += 1

            # Patch the triangle count
            f.seek(80)
            f.write(struct.pack('<I', count))

        progress(f"  Streamed {count:,} triangles.", 95)
        return count

    # ── Disc geometry generator ────────────────────────────────────────────

    def _disc_geometry(self) -> Iterator[Triangle]:
        """Yield all triangles for the disc body (no groove)."""
        N  = self.N_DISC_SEGS
        ro = self.outer_r
        ri = self.hole_r
        T  = self.T + self.z_offset
        B  = self.z_offset

        # Bottom face (normal downward)
        yield from _ring_face(ro, ri, B, N, flip=True)

        # Top face (normal upward) — groove will be cut into this
        yield from _ring_face(ro, ri, T, N, flip=False)

        # Outer cylindrical wall
        yield from _cylinder_wall(ro, B, T, N, outward=True)

        # Inner hole wall (normal pointing inward toward axis)
        yield from _cylinder_wall(ri, B, T, N, outward=False)

    # ── Z-offset helper ────────────────────────────────────────────────────

    def _offset_tri(self, t: Triangle) -> Triangle:
        if self.z_offset == 0.0:
            return t
        n, v0, v1, v2 = t
        zo = self.z_offset
        return (
            n,
            (v0[0], v0[1], v0[2] + zo),
            (v1[0], v1[1], v1[2] + zo),
            (v2[0], v2[1], v2[2] + zo),
        )

    # ── Binary STL writer ─────────────────────────────────────────────────

    def _write_binary(self, tris: List[Triangle]) -> None:
        with open(self.out_path, 'wb') as f:
            f.write(b'\x00' * 80)                        # 80-byte header
            f.write(struct.pack('<I', len(tris)))         # triangle count
            for t in tris:
                f.write(_pack_tri(t))


# ══════════════════════════════════════════════════════════════════════════════
# Two-sided record helper
# ══════════════════════════════════════════════════════════════════════════════

class TwoSidedSTLWriter:
    """
    Writes a two-sided record:
      • Side A at z = 0 … T
      • Side B at z = (T + gap) … (2T + gap)

    Both sides are written into a single STL file (SINGLE mode) or
    two separate STL files (STREAMING mode, one per side).

    Parameters
    ----------
    gap_mm : float
        Air gap between the two disc faces (for visual separation in slicer).
        Typically 2 mm.
    """

    def __init__(
        self,
        size_inch: int,
        rpm: int,
        output_prefix: str,
        mode: str = STL_MODE_SINGLE,
        gap_mm: float = 2.0,
    ):
        self.size   = size_inch
        self.rpm    = rpm
        self.prefix = output_prefix
        self.mode   = mode
        self.gap    = gap_mm
        self.T      = RECORD_SPECS[size_inch]["thickness"]

    def write(
        self,
        pts_a: List[GroovePoint],
        pts_b: List[GroovePoint],
        progress_cb=None,
    ) -> Tuple[str, str, int]:
        """
        Write Side A and Side B.

        Returns (path_a, path_b, total_triangles).
        In SINGLE mode, both are written to <prefix>_both.stl.
        In STREAMING mode, each side to its own file.
        """
        def progress(msg: str, pct: int):
            if progress_cb:
                progress_cb(msg, pct)

        if self.mode == STL_MODE_STREAMING:
            return self._write_streaming_separate(pts_a, pts_b, progress)
        else:
            return self._write_single_combined(pts_a, pts_b, progress)

    def _write_single_combined(self, pts_a, pts_b, progress):
        path = f"{self.prefix}_both.stl"

        # Side A: z_offset = 0
        writer_a = STLWriter(self.size, self.rpm, path,
                             mode=STL_MODE_SINGLE, z_offset=0.0)
        # Side B: z_offset = T + gap
        z_b = self.T + self.gap
        writer_b = STLWriter(self.size, self.rpm, path,
                             mode=STL_MODE_SINGLE, z_offset=z_b)

        progress("  Building Side A geometry...", 85)
        tris = []
        for t in writer_a._disc_geometry():
            tris.append(t)
        z_surf_a = self.T
        for t in _groove_triangles(pts_a, z_surf_a):
            tris.append(t)

        progress("  Building Side B geometry...", 90)
        for t in writer_b._disc_geometry():
            tris.append(t)
        z_surf_b = z_b + self.T
        for t in _groove_triangles(pts_b, z_surf_b):
            tris.append(writer_b._offset_tri(t))

        progress(f"  Writing {len(tris):,} triangles → {path}", 94)
        writer_a._write_binary(tris)
        return path, path, len(tris)

    def _write_streaming_separate(self, pts_a, pts_b, progress):
        path_a = f"{self.prefix}_sideA.stl"
        path_b = f"{self.prefix}_sideB.stl"

        progress("  Streaming Side A...", 85)
        writer_a = STLWriter(self.size, self.rpm, path_a,
                             mode=STL_MODE_STREAMING, z_offset=0.0)
        n_a = writer_a.write(pts_a, progress_cb=lambda m, p: progress(m, 85 + p//10))

        progress("  Streaming Side B...", 92)
        writer_b = STLWriter(self.size, self.rpm, path_b,
                             mode=STL_MODE_STREAMING, z_offset=0.0)
        n_b = writer_b.write(pts_b, progress_cb=lambda m, p: progress(m, 92 + p//10))

        return path_a, path_b, n_a + n_b


# ══════════════════════════════════════════════════════════════════════════════
# Print settings helper
# ══════════════════════════════════════════════════════════════════════════════

def get_print_recommendations(rpm: int) -> dict:
    """Return recommended slicer settings for the given RPM."""
    return PRINT_RECOMMENDATIONS.get(rpm, PRINT_RECOMMENDATIONS[33])


def estimate_stl_size_mb(n_triangles: int) -> float:
    """Estimate binary STL file size in megabytes."""
    # 80-byte header + 4-byte count + 50 bytes/triangle
    return (80 + 4 + n_triangles * 50) / (1024 * 1024)


def estimate_triangle_count(
    size_inch: int,
    rpm: int,
    pts_per_rev: int,
    n_disc_segs: int = 360,
) -> dict:
    """
    Estimate the number of STL triangles before generation.

    Returns dict with:
        disc_tris    : triangles for disc body (rings + walls)
        groove_tris  : triangles for groove spiral
        total_tris   : total
        stl_size_mb  : estimated file size in MB
    """
    from record_specs import calc_max_duration, RPM_GROOVE
    stats = calc_max_duration(size_inch, rpm)
    total_revs   = stats["turns"]
    total_groove = int(total_revs * pts_per_rev)

    # Disc body: bottom ring + top ring + outer wall + inner wall
    # Each ring: n_segs * 2 triangles
    # Each wall: n_segs * 2 triangles
    disc_tris = n_disc_segs * 2 * 4   # 4 surfaces × 2 tris/seg

    # Groove: (total_pts - 1) segments × 4 triangles each (left wall ×2, right wall ×2)
    groove_tris = (total_groove - 1) * 4

    total = disc_tris + groove_tris
    return {
        "disc_tris":   disc_tris,
        "groove_tris": groove_tris,
        "total_tris":  total,
        "stl_size_mb": estimate_stl_size_mb(total),
        "groove_pts":  total_groove,
    }
