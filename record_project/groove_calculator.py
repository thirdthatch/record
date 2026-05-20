"""
groove_calculator.py
--------------------
IEC 45/45 stereo groove path calculation for vinyl record cutting.

The IEC 60098 / RIAA 45/45 stereo standard:
  - Groove is cut at 45° for both channels
  - Left channel modulates the inner (left) wall
  - Right channel modulates the outer (right) wall
  - Groove cross-section is a 90° V-shape

Modulation equations:
  lateral  = (L + R) / sqrt(2)   ← horizontal movement of groove center
  vertical = (L - R) / sqrt(2)   ← vertical movement of groove center

Each groove point is defined by:
  (x, y)      : center position on disc surface
  d_lat       : lateral displacement from groove center
  d_vert      : vertical displacement from nominal depth
  wall_left   : actual 3D position of left groove wall
  wall_right  : actual 3D position of right groove wall
"""

from __future__ import annotations
import math
from typing import List, Tuple, Iterator, NamedTuple
from dataclasses import dataclass

from record_specs import (
    RECORD_SPECS, RPM_GROOVE,
    STEREO_45_SCALE, MAX_DISPLACEMENT_FRACTION,
    GROOVE_PTS_PER_REV, GROOVE_SAMPLE_RATE,
    calc_max_duration,
)


# ── Data types ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class GroovePoint:
    """
    A single point along the groove spiral.

    x, y        : groove center position (mm) on disc top surface
    z_left      : z height of left wall top edge (mm from bottom of disc)
    z_right     : z height of right wall top edge (mm from bottom of disc)
    x_left      : x position of left wall top edge
    y_left      : y position of left wall top edge
    x_right     : x position of right wall top edge
    y_right     : y position of right wall top edge
    z_floor     : z height of groove floor (mm from bottom of disc)
    """
    x:       float
    y:       float
    z_floor: float
    x_left:  float
    y_left:  float
    x_right: float
    y_right: float



GroovePath = List[GroovePoint]


# ══════════════════════════════════════════════════════════════════════════════
# Groove Path Calculator
# ══════════════════════════════════════════════════════════════════════════════

class GrooveCalculator:
    """
    Generates the 3D spiral groove path from audio samples.

    Implements IEC 60098 / RIAA 45/45 stereo standard.
    """

    def __init__(
        self,
        size_inch: int,
        rpm: int,
        groove_mode: str,         # "mono" or "stereo"
        quality: str = "high",    # key from GROOVE_PTS_PER_REV
        side: str = "A",          # "A" (outer→inner) or "B" (reversed audio)
        groove_spacing_factor: float = 1.0,  # multiplier for groove spacing
    ):
        self.size   = size_inch
        self.rpm    = rpm
        self.mode   = groove_mode
        self.side   = side

        self.spec   = RECORD_SPECS[size_inch]
        self.groove = RPM_GROOVE[rpm]
        self.pts_per_rev = GROOVE_PTS_PER_REV[quality]

        # Geometry
        self.r_out   = self.spec["groove_outer_r"]
        self.r_in    = self.spec["groove_inner_r"]
        self.T       = self.spec["thickness"]
        # Apply groove spacing factor to calculate pitch
        adjusted_spacing = self.groove["groove_spacing"] * groove_spacing_factor
        self.pitch   = self.groove["groove_width"] + adjusted_spacing
        self.depth   = self.groove["groove_depth"]
        self.half_w  = self.groove["groove_width"] / 2.0
        self.angle_r = math.radians(self.groove["cutting_angle"])  # 45° → π/4

        # Max displacement before groove walls cross
        self.max_disp = self.half_w * MAX_DISPLACEMENT_FRACTION

        stats = calc_max_duration(size_inch, rpm, groove_spacing_factor)
        self.turns    = stats["turns"]
        self.max_dur  = stats["duration_s"]

    # ── Main generator ─────────────────────────────────────────────────────

    def generate(
        self,
        left:  List[float],
        right: List[float],
        sample_rate: int,
        progress_cb=None,
    ) -> Iterator[GroovePoint]:
        """
        Generate GroovePoints as an iterator (memory efficient).

        Caller can collect into list or pass directly to STL writer.
        """
        n_samples  = min(len(left), len(right))
        samp_per_rev = sample_rate * 60.0 / self.rpm
        total_revs   = min(n_samples / samp_per_rev, self.turns)
        total_pts    = int(total_revs * self.pts_per_rev)

        if self.side == "B":
            # Side B: reverse both channels so audio plays outer→inner
            left  = list(reversed(left))
            right = list(reversed(right))

        report_interval = max(1, total_pts // 20)

        for i in range(total_pts):
            frac    = i / total_pts
            angle   = frac * total_revs * 2.0 * math.pi  # radians, CCW
            r_center = self.r_out - frac * total_revs * self.pitch

            # ── Sample audio at this groove position ───────────────────────
            si = int(frac * total_revs * samp_per_rev)
            si = min(si, n_samples - 1)
            L  = left[si]
            R  = right[si]

            # ── IEC 45/45 modulation ───────────────────────────────────────
            if self.mode == "stereo":
                # Standard 45/45: lateral + vertical components
                d_lat  = (L + R) * STEREO_45_SCALE  # lateral displacement
                d_vert = (L - R) * STEREO_45_SCALE  # vertical displacement
            else:
                # Mono: pure lateral (hill-and-dale for vertical, lateral for mono)
                d_lat  = (L + R) / 2.0
                d_vert = 0.0

            # Scale to physical units
            d_lat  = d_lat  * self.max_disp
            d_vert = d_vert * self.depth * 0.35  # ±35% of groove depth

            # Clamp to prevent overcut
            d_lat  = max(-self.max_disp, min(self.max_disp, d_lat))
            d_vert = max(-self.depth * 0.4, min(self.depth * 0.4, d_vert))

            # ── Groove center position ──────────────────────────────────────
            # Angle is negated so spiral goes clockwise (standard LP direction)
            cos_a = math.cos(-angle)
            sin_a = math.sin(-angle)

            # Direction along groove (tangent vector)
            tx = -sin_a   # d(cos(-angle))/d(angle) = sin(-angle) but reversed
            ty =  cos_a

            # Normal to groove direction (pointing outward laterally)
            nx =  cos_a
            ny =  sin_a

            # Groove center
            cx = r_center * cos_a + d_lat * nx
            cy = r_center * sin_a + d_lat * ny

            # Z positions
            z_nominal_floor = self.T - self.depth
            z_floor         = z_nominal_floor + d_vert
            z_floor         = max(0.05, min(z_floor, self.T - 0.05))

            # ── Wall positions (45° V-groove) ──────────────────────────────
            # Left wall (inner wall of groove, 45° from vertical)
            # The wall top is at surface level (T), base at groove floor
            # Lateral offset of wall top from center = groove_depth * tan(45°) = depth
            wall_offset = self.depth  # for 45° walls: tan(45°)=1, so offset=depth

            # Left wall top (inner side)
            xleft  = cx - nx * (self.half_w + wall_offset * 0.0)
            yleft  = cy - ny * (self.half_w + wall_offset * 0.0)

            # Right wall top (outer side)
            xright = cx + nx * (self.half_w + wall_offset * 0.0)
            yright = cy + ny * (self.half_w + wall_offset * 0.0)

            # Exact wall top edges at surface
            xleft_top  = cx - nx * self.half_w
            yleft_top  = cy - ny * self.half_w
            xright_top = cx + nx * self.half_w
            yright_top = cy + ny * self.half_w

            pt = GroovePoint(
                x=cx, y=cy,
                z_floor=z_floor,
                x_left=xleft_top, y_left=yleft_top,
                x_right=xright_top, y_right=yright_top,
            )

            yield pt

            if progress_cb and (i % report_interval == 0):
                pct = 50 + int(35 * i / total_pts)
                progress_cb(f"  Groove point {i:,} / {total_pts:,}", pct)

    # ── Statistics ────────────────────────────────────────────────────────

    def groove_stats(self, n_samples: int, sample_rate: int) -> dict:
        samp_per_rev = sample_rate * 60.0 / self.rpm
        actual_revs  = min(n_samples / samp_per_rev, self.turns)
        total_pts    = int(actual_revs * self.pts_per_rev)
        actual_dur   = n_samples / sample_rate

        # Estimate groove length
        r_mean     = (self.r_out + self.r_in) / 2.0
        groove_len = 2.0 * math.pi * r_mean * actual_revs

        return {
            "actual_duration_s": actual_dur,
            "max_duration_s":    self.max_dur,
            "turns":             actual_revs,
            "groove_len_mm":     groove_len,
            "total_groove_pts":  total_pts,
            "pts_per_rev":       self.pts_per_rev,
            "pitch_mm":          self.pitch,
        }
