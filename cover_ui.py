# =============================================================================
#  cover_ui.py  ^`^t pure text builders, no Home Assistant access.
#  Turns raw facts (a boundary key, a status kind, a number of minutes) into
#  the human-readable strings shown on the boundary and status sensors.
#  Imported by cover_control.py.
# =============================================================================

_BOUNDARY_LABELS = {
    "cloud":         "Cloudy (open)",
    "max_elevation": "Sun too high (open)",
    "min_elevation": "Sun too low (open)",
    "fov_left":      "Sun past left edge (open)",
    "fov_right":     "Sun past right edge (open)",
    "clear":         "Shading",
}


def boundary_text(boundary):
    """Readable label for the boundary sensor."""
    return _BOUNDARY_LABELS.get(boundary, boundary)


def status_text(kind, minutes=None, target=None):
    """
    Readable label for the status sensor.

    kind is one of:
        master_off, override, stable, position_deadband,
        time_deadband, moving, error
    minutes  fills the countdown for override / time_deadband
    target   fills the % for moving, or the message for error
    """
    if kind == "master_off":
        return "Disabled (master)"
    if kind == "override":
        return f"Override ({minutes}m)" if minutes is not None else "Override"
    if kind == "stable":
        return "Stable"
    if kind == "position_deadband":
        return "Position deadband"
    if kind == "time_deadband":
        return f"Time deadband ({minutes}m)" if minutes is not None else "Time deadband"
    if kind == "moving":
        return f"Moving to {target}%"
    if kind == "error":
        return f"Error: {target}"
    return kind
