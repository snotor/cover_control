import math

# =============================================================================
#  cover_geometry.py  ^`^t pure functions, no Home Assistant access.
#  Imported by cover_control.py. Everything here is unit-testable in plain
#  Python (see the test harness).
# =============================================================================


def compute_target(sun_az, sun_el, cfg):
    """
    Work out where the blind should sit to limit direct sun to cfg["depth"]
    metres of floor penetration.

    Args:
        sun_az, sun_el : sun azimuth and elevation, degrees (from sun.sun).
        cfg            : dict with azimuth, height, depth, elev_min, elev_max,
                         fov_left, fov_right, pos_min, pos_max, snap_open.

    Returns:
        (raw_position, boundary)
          raw_position : shading position 0-100 the geometry wants, clamped to
                         the travel limits. Reported even when a boundary is
                         active  ^`^t this is the "target_position" sensor.
          boundary     : one of "max_elevation", "min_elevation",
                         "fov_left", "fov_right", "clear".  (Cloud is decided
                         by the control layer, which ranks above all of these.)
    """
    # Horizontal offset of the sun from window centre, normalised to -180..180.
    offset = (sun_az - cfg["azimuth"] + 180) % 360 - 180

    # Cap the field of view just under 90   so the cos() below never hits zero.
    fov_left  = min(cfg["fov_left"], 89.9)
    fov_right = min(cfg["fov_right"], 89.9)

    # --- Raw shading target (only defined when the sun is up and in front) ---
    if sun_el > 0 and abs(offset) < 90:
        # Profile (vertical shadow) angle: effective elevation in the plane
        # perpendicular to the glass, corrected for the sun being off to the side.
        profile = math.atan(
            math.tan(math.radians(sun_el)) / math.cos(math.radians(offset))
        )
        # Fraction of the window we can leave uncovered while keeping direct sun
        # within cfg["depth"] metres of penetration. 0 = must fully cover, 1 = may
        # fully open.
        open_frac = cfg["depth"] * math.tan(profile) / cfg["height"]
        open_frac = max(0.0, min(1.0, open_frac))

        # Map that fraction onto the cover's REAL travel range:
        #   pos_min = the position at which the window is fully covered (max
        #             shading). On some covers this is not 0%  ^`^t e.g. a shutter
        #             that reaches the sill at 40% and only seals its slats below
        #             that. We never command below pos_min, so the 0..pos_min
        #             "sealing" zone is never used for shading.
        #   pos_max = window fully open.
        raw = cfg["pos_min"] + open_frac * (cfg["pos_max"] - cfg["pos_min"])
        if raw >= cfg["snap_open"]:
            raw = cfg["pos_max"]
    else:
        # Sun below horizon or behind the glass -> nothing to shade.
        raw = cfg["pos_max"]

    # --- Boundary reason, in priority order ---
    if sun_el >= cfg["elev_max"]:
        boundary = "max_elevation"
    elif sun_el <= cfg["elev_min"]:
        boundary = "min_elevation"
    elif offset < -fov_left:
        boundary = "fov_left"
    elif offset > fov_right:
        boundary = "fov_right"
    else:
        boundary = "clear"

    return round(raw), boundary
