"""
cover_control.py - adaptive room shading for Home Assistant covers (AppDaemon).
One app instance per window (one block per window in apps.yaml). Positions the
cover from sun geometry to limit direct sun to a set penetration depth, with
optional cloud handling (idle when overcast), manual override, deadbands and a
move watchdog.

While shading it drives the cover to the geometric target. When shading isn't
needed (sun too low/high, off to the side, or cloudy) it opens the cover once
and then latches into "idle" - it stops touching the cover and leaves it where
it is. Idle is entered only after that open move reaches its target or errors
out. If a shading move fails to reach target it retries on later ticks; after
max_retries consecutive failures it gives up and goes idle too. Moving the
cover by hand while the sun is on the window (shading, or held open while
cloudy) arms the manual override and its timer, handing control back to you;
moving it when the sun is out of bounds (e.g. at night) does not.

Publishes three auto-named sensors (<name> = block key, or the `name:` arg):
  sensor.<name>_target_position  shading % the geometry wants (ignores boundaries)
  sensor.<name>_boundary         why it is / isn't shading
  sensor.<name>_status           control state (+ countdowns)

The cover must support set_cover_position (0 = closed, 100 = open). For an
inverted cover (0 = open, 100 = closed), swap position_min/position_max
(e.g. position_min: 100, position_max: 0) - the rest adapts automatically.

apps.yaml keys
REQUIRED:
  module / class      cover_control / CoverControl
  cover_entity        the cover to drive
  master_switch       app-wide enable input_boolean
  override_bool       this window's manual-override input_boolean
  window_azimuth      bearing the window faces, degrees
  window_height       glass height, metres (invalid/<=0 falls back to 1.0 + warns)
OPTIONAL (default):
  name (block key)    slug for the sensors
  cloud_sensor        cloud coverage sensor, 0-100 (omit to skip the cloud check)
  pos_sensor          (cover current_position) position source, 0-100
  sun_depth      0.20 allowed sun penetration onto floor, metres
  elevation_min  25   sun below this -> stop shading (idle)
  elevation_max  70   sun above this -> stop shading (idle)
  fov_left / fov_right 70 deg off-centre still shaded
  position_min   30   position where the window is fully covered
  position_max   100  fully open
  snap_open      80   target >= this -> snap fully open
  min_diff       5    smallest % change worth moving for
  max_retries    3    failed shading moves before giving up to idle (<=0 = never)
  midnight_reset true clear the give-up latch at midnight each day (false: keep
                       until master toggled / restart)
  move_cooldown  600  seconds between auto-moves (time deadband)
  override_duration 7200 seconds before override auto-clears
  cloud_threshold 80  cloud % above which it stops shading (goes idle)
  cloud_clear_threshold 70 cloud % below which shading resumes (hysteresis)
  move_start_timeout 10 seconds to wait for movement to begin
  move_timeout   30   seconds max to reach target
  tick_interval  30   seconds between recompute + UI refresh
"""

import datetime
import math

DEFAULT_WINDOW_HEIGHT = 1.0  # metres; fallback when window_height is missing/invalid

try:
    import hassapi as hass
    _Base = hass.Hass
except ImportError:  # lets the pure helpers import without AppDaemon
    _Base = object


# ---------------------------------------------------------------------------
# Pure geometry - no HA access, unit-testable
# ---------------------------------------------------------------------------
def compute_target(sun_az, sun_el, cfg):
    """Return (position, boundary). position lands within [pos_min, pos_max];
    boundary is max_elevation / min_elevation / fov_left / fov_right / clear."""
    # Sun offset from window centre, -180..180.
    offset = (sun_az - cfg["azimuth"] + 180) % 360 - 180
    fov_left = min(cfg["fov_left"], 89.9)   # keep cos() below off zero
    fov_right = min(cfg["fov_right"], 89.9)

    # Shading position - only meaningful with the sun up and in front.
    if sun_el > 0 and abs(offset) < 90:
        # Profile angle = effective elevation in the plane facing the glass.
        profile = math.atan(math.tan(math.radians(sun_el)) /
                            math.cos(math.radians(offset)))
        # Fraction of window we may leave uncovered for `depth` m of sun.
        frac = cfg["depth"] * math.tan(profile) / cfg["height"]
        frac = max(0.0, min(1.0, frac))
        # Snap to fully open once the window is snap_open% uncovered. Tested on
        # the fraction, not the position, so it works whichever way the cover
        # counts (0=closed/100=open or the reverse via swapped pos_min/pos_max).
        if frac * 100 >= cfg["snap_open"]:
            raw = cfg["pos_max"]
        else:
            # Map onto real travel: pos_min = fully covered, pos_max = fully open.
            raw = cfg["pos_min"] + frac * (cfg["pos_max"] - cfg["pos_min"])
    else:
        raw = cfg["pos_max"]  # nothing to shade

    # Boundary, priority order (cloud is added by the caller, above these).
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


# ---------------------------------------------------------------------------
# Pure text builders
# ---------------------------------------------------------------------------
_BOUNDARY_LABELS = {
    "cloud": "Cloudy",
    "max_elevation": "Sun too high",
    "min_elevation": "Sun too low",
    "fov_left": "Sun past left edge",
    "fov_right": "Sun past right edge",
    "clear": "Shading",
}


def boundary_text(boundary):
    return _BOUNDARY_LABELS.get(boundary, boundary)


def status_text(kind, minutes=None, target=None):
    """kind: master_off / override / idle / stable / position_deadband /
    time_deadband / moving / error."""
    if kind == "master_off":
        return "Disabled (master)"
    if kind == "override":
        return f"Override ({minutes}m)" if minutes is not None else "Override"
    if kind == "idle":
        return f"Idle ({target})" if target else "Idle"
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


# ---------------------------------------------------------------------------
# AppDaemon control app
# ---------------------------------------------------------------------------
class CoverControl(_Base):

    def initialize(self):
        a = self.args
        # Wiring
        self.cover_entity = a["cover_entity"]
        self.master_switch = a["master_switch"]
        self.override_bool = a["override_bool"]
        self.cloud_sensor = a.get("cloud_sensor")  # optional; None skips the cloud check

        # Position source: dedicated sensor, else the cover's own attribute.
        pos_sensor = a.get("pos_sensor")
        if pos_sensor:
            self.pos_entity, self.pos_attr = pos_sensor, None
        else:
            self.pos_entity, self.pos_attr = self.cover_entity, "current_position"

        # window_height is required; guard against 0 / negative / non-numeric
        # (0 would divide-by-zero every tick) with a safe default and a warning.
        try:
            height = float(a["window_height"])
        except (KeyError, TypeError, ValueError):
            height = 0.0
        if height <= 0:
            self.log(f"window_height must be positive; got {a.get('window_height')!r}, "
                     f"using {DEFAULT_WINDOW_HEIGHT} m - please fix apps.yaml.")
            height = DEFAULT_WINDOW_HEIGHT

        # Geometry config (passed straight to compute_target)
        self.cfg = {
            "azimuth": float(a["window_azimuth"]),
            "height": height,
            "depth": float(a.get("sun_depth", 0.20)),
            "elev_min": float(a.get("elevation_min", 25)),
            "elev_max": float(a.get("elevation_max", 70)),
            "fov_left": float(a.get("fov_left", 70)),
            "fov_right": float(a.get("fov_right", 70)),
            "pos_min": float(a.get("position_min", 30)),
            "pos_max": float(a.get("position_max", 100)),
            "snap_open": float(a.get("snap_open", 80)),
        }

        # Control tunables
        self.min_diff = float(a.get("min_diff", 5))
        self.cooldown = float(a.get("move_cooldown", 600))
        self.override_dur = float(a.get("override_duration", 7200))
        self.cloud_threshold = float(a.get("cloud_threshold", 80))
        self.cloud_clear = float(a.get("cloud_clear_threshold", 70))
        if self.cloud_clear >= self.cloud_threshold:
            self.cloud_clear = self.cloud_threshold - 1  # keep the band valid
            self.log("cloud_clear_threshold must be below cloud_threshold; adjusted.")
        self.move_start_to = float(a.get("move_start_timeout", 10))
        self.move_to = float(a.get("move_timeout", 30))
        self.tick_interval = float(a.get("tick_interval", 30))
        self.max_retries = int(a.get("max_retries", 3))  # failed shading moves before
        #                                                  giving up to idle; <=0 = infinite
        # Automatically clear the give-up latch at midnight each day, so a cover
        # that was stuck yesterday gets retried with a clean slate at the next
        # shading opportunity. Off = stay given-up until master toggled / restart.
        self.midnight_reset = a.get("midnight_reset", True)
        if isinstance(self.midnight_reset, str):
            self.midnight_reset = self.midnight_reset.strip().lower() in ("true", "1", "yes", "on")

        # Auto-derived sensor names
        slug = a.get("name", self.name).lower().replace(" ", "_")
        self.s_target = f"sensor.{slug}_target_position"
        self.s_boundary = f"sensor.{slug}_boundary"
        self.s_status = f"sensor.{slug}_status"

        # State
        now = datetime.datetime.now()
        self.last_move_time = now - datetime.timedelta(seconds=self.cooldown)
        self.override_start = now
        self.cloud_open = False
        self.shading = False
        self.sun_in_front = False   # geometric: sun within elevation range + fov
        self.idle = False           # True once the not-shading open move has resolved
        self.gave_up = False        # True after max_retries failed shading moves
        self.fail_count = 0         # consecutive failed shading moves this episode
        self.move_is_open = False   # is the in-flight app move the not-shading open?
        self.moving_by_app = False
        self.commanded_target = None
        self.pos_at_command = None
        self.retry_done = False
        self.override_handle = None
        self.start_handle = None
        self.arrive_handle = None

        # Subscriptions
        self.listen_state(self.on_master, self.master_switch)
        if self.cloud_sensor:
            self.listen_state(self.evaluate, self.cloud_sensor)
        self.listen_state(self.on_override, self.override_bool)
        if self.pos_attr:
            self.listen_state(self.on_position, self.pos_entity, attribute=self.pos_attr)
        else:
            self.listen_state(self.on_position, self.pos_entity)
        self.run_every(self.evaluate, now, self.tick_interval)  # sun moves continuously
        if self.midnight_reset:
            self.run_daily(self._midnight_reset, "00:00:00")

        # Re-arm a persisted override so it can't latch on across a restart.
        if self.get_state(self.override_bool) == "on":
            self.override_handle = self.run_in(self._clear_override, self.override_dur)

        self.log(f"CoverControl '{slug}' online -> {self.cover_entity}")
        if self.get_state(self.master_switch) == "on":
            self.evaluate()  # last_move_time in the past -> sync now

    # ---- main evaluation: publish sensors, move when clear ----
    def evaluate(self, *args, force=False):
        # force=True (master re-enable) skips the position + time deadbands so the
        # cover jumps straight to target instead of waiting out a cooldown/min_diff.
        az = self._num("sun.sun", attr="azimuth")
        el = self._num("sun.sun", attr="elevation")
        cloud = self._num(self.cloud_sensor) if self.cloud_sensor else None
        if az is None or el is None:
            self._publish_status("error", target="sun data unavailable")
            return

        raw, boundary = compute_target(az, el, self.cfg)
        # Geometric boundary (before cloud): "clear" means the sun is actually on
        # this window. Used to decide whether a hand-move should arm the override.
        self.sun_in_front = boundary == "clear"

        # Cloud outranks the geometric boundaries; hysteresis stops flapping.
        # Skipped entirely when no cloud_sensor is configured.
        if cloud is not None:
            if self.cloud_open and cloud < self.cloud_clear:
                self.cloud_open = False
            elif not self.cloud_open and cloud > self.cloud_threshold:
                self.cloud_open = True
        if self.cloud_open:
            boundary = "cloud"

        self.shading = boundary == "clear"
        if self.shading:
            self.idle = False       # shading resumed -> a fresh open is owed next time

        # Display sensors update regardless of control state.
        self.set_state(self.s_target, state=raw,
                       attributes={"unit_of_measurement": "%", "icon": "mdi:window-shutter"})
        self.set_state(self.s_boundary, state=boundary_text(boundary),
                       attributes={"reason": boundary, "icon": "mdi:sun-angle"})

        # Status + movement decisions.
        if self.get_state(self.master_switch) == "off":
            self._publish_status("master_off")
            return
        if self.get_state(self.override_bool) == "on":
            self._publish_status("override",
                                 minutes=self._mins(self.override_start, self.override_dur))
            return

        if self.moving_by_app:  # an app move (shading or the open) is in flight
            self._publish_status("moving", target=self.commanded_target)
            return

        # Idle: either we've finished the not-shading open and are hands-off, or a
        # shading move failed max_retries times and we gave up. Both latches are
        # only set by the watchdogs (after a move resolves), so we never report
        # idle before at least attempting to move.
        if self.gave_up:
            self._publish_status("idle", target="cover unresponsive")
            return
        if not self.shading and self.idle:
            self._publish_status("idle")
            return

        current = self._current_pos()
        if current is None:
            self._publish_status("error", target="position unavailable")
            return

        # Shading -> the geometric target; not shading -> fully open.
        target = raw if self.shading else self.cfg["pos_max"]
        diff = abs(target - current)
        if diff < 1:
            self._settle("stable")
            return
        if diff < self.min_diff and not force:
            self._settle("position_deadband")
            return

        elapsed = (datetime.datetime.now() - self.last_move_time).total_seconds()
        if elapsed < self.cooldown and not force:
            self._publish_status("time_deadband",
                                 minutes=math.ceil((self.cooldown - elapsed) / 60))
            return  # the periodic tick retries later

        self._issue_move(target)

    # ---- movement + watchdogs ----
    def _issue_move(self, target):
        self.pos_at_command = self._current_pos()
        if self.pos_at_command is not None and abs(target - self.pos_at_command) < 1:
            self._settle("stable")
            return
        self.move_is_open = not self.shading  # the not-shading open vs a shading move
        self._cancel_watchdogs()
        self.commanded_target = round(target)
        self.moving_by_app = True
        self.retry_done = False
        self.last_move_time = datetime.datetime.now()
        self._publish_status("moving", target=self.commanded_target)
        self.call_service("cover/set_cover_position",
                          entity_id=self.cover_entity, position=self.commanded_target)
        self.start_handle = self.run_in(self._check_started, self.move_start_to)

    def _check_started(self, kwargs):
        self.start_handle = None
        if not self.moving_by_app:
            return
        cur = self._current_pos()
        if cur is None:
            # Position momentarily unavailable - don't wedge in "moving". Hand off
            # to the arrival check, which tolerates an unknown position and resolves.
            self.arrive_handle = self.run_in(self._check_arrived, self.move_to)
            return
        moved = self.pos_at_command is None or abs(cur - self.pos_at_command) >= 1
        if moved:
            self.arrive_handle = self.run_in(self._check_arrived, self.move_to)
        elif not self.retry_done:  # cover didn't budge -> retry once
            self.retry_done = True
            self.call_service("cover/set_cover_position",
                              entity_id=self.cover_entity, position=self.commanded_target)
            self.start_handle = self.run_in(self._check_started, self.move_start_to)
        else:
            self.moving_by_app = False
            self.commanded_target = None
            if self.move_is_open:      # open never started -> give up, go idle
                self.idle = True
                self.evaluate()
            else:
                self._shading_move_failed("no response")

    def _shading_move_failed(self, reason):
        # A shading move didn't reach target. Surface it as a UI message and let
        # the next tick try again. After max_retries consecutive failures, give up
        # and go idle instead. max_retries <= 0 means never give up (retry forever).
        self.fail_count += 1
        if self.max_retries > 0 and self.fail_count >= self.max_retries:
            self.gave_up = True
            self._publish_status("idle", target="cover unresponsive")
        else:
            self._publish_status("error", target=reason)

    def _check_arrived(self, kwargs):
        self.arrive_handle = None
        if not self.moving_by_app:
            return
        cur = self._current_pos()
        target = self.commanded_target
        was_open = self.move_is_open
        self.moving_by_app = False
        self.commanded_target = None
        if cur is not None and abs(cur - target) < self.min_diff:
            self.evaluate()            # arrived -> stable (shading) or idle (open)
        elif was_open:
            self.idle = True           # open finished/errored -> idle, leave it be
            self.evaluate()
        else:
            # Shading move didn't reach target -> count it (retry, or give up to
            # idle after max_retries). Purely a UI signal; never stops the ticks.
            self._shading_move_failed("not reached")

    # ---- manual-move detection ----
    def on_position(self, entity, attribute, old, new, kwargs):
        if self.get_state(self.master_switch) == "off":
            return
        if self.get_state(self.override_bool) == "on":
            return
        try:
            old_f, new_f = float(old), float(new)
        except (TypeError, ValueError):
            return
        if abs(new_f - old_f) < 1:
            return  # jitter
        if self.moving_by_app:
            if abs(new_f - self.commanded_target) < self.min_diff:  # our move arrived
                self._cancel_watchdogs()
                self.moving_by_app = False
                self.commanded_target = None
                self.evaluate()
        else:
            # Arm the override only when the sun is on this window - i.e. shading,
            # or open while cloudy with the sun still in view. Then a hand-move is a
            # deliberate "leave it as I set it" (e.g. closing the blind for a movie
            # while it's held open for clouds, so it stays put when the cloud clears).
            # When the sun is out of bounds (night, or round the other side) the
            # app isn't shading anyway, so moving the cover is just normal use and
            # must not lock the app out.
            if self.sun_in_front:
                self._activate_override()

    # ---- override + master ----
    def _activate_override(self):
        self.turn_on(self.override_bool)  # rest handled by on_override

    def on_override(self, entity, attribute, old, new, kwargs):
        if self.override_handle:
            self.cancel_timer(self.override_handle)
            self.override_handle = None
        if new == "on":
            self.override_start = datetime.datetime.now()
            self.override_handle = self.run_in(self._clear_override, self.override_dur)
        self.evaluate()

    def _clear_override(self, kwargs):
        self.override_handle = None
        self.turn_off(self.override_bool)  # -> on_override("off") -> evaluate

    def _midnight_reset(self, kwargs):
        # Clears the give-up latch once per day so a stuck cover from yesterday
        # is retried fresh at the next shading opportunity.
        if self.gave_up:
            self.gave_up = False
            self.fail_count = 0
            self.log("Midnight reset: cleared give-up latch.")
            self.evaluate()

    def on_master(self, entity, attribute, old, new, kwargs):
        self.gave_up = False   # a master toggle always clears the give-up latch
        self.fail_count = 0
        if new == "on":
            self.evaluate(force=True)  # re-enable -> jump to target, skip deadbands
        else:
            self._cancel_watchdogs()
            self.moving_by_app = False
            self.commanded_target = None
            self._publish_status("master_off")

    # ---- helpers ----
    def _settle(self, shading_status):
        # At (or within deadband of) target -> nothing to move. While shading that's
        # `shading_status` (stable / position_deadband); otherwise the not-shading
        # open is complete, so latch idle.
        if self.shading:
            self.fail_count = 0
            self._publish_status(shading_status)
        else:
            self.idle = True
            self._publish_status("idle")

    def _publish_status(self, kind, minutes=None, target=None):
        icon = ("mdi:robot-confused" if kind == "error"
                else "mdi:robot-off" if kind == "idle"
                else "mdi:robot")
        self.set_state(self.s_status,
                       state=status_text(kind, minutes=minutes, target=target),
                       attributes={"kind": kind, "icon": icon})

    def _mins(self, start_time, duration):
        elapsed = (datetime.datetime.now() - start_time).total_seconds()
        return math.ceil(max(0, duration - elapsed) / 60)

    def _cancel_watchdogs(self):
        for h in ("start_handle", "arrive_handle"):
            if getattr(self, h):
                self.cancel_timer(getattr(self, h))
                setattr(self, h, None)

    def _current_pos(self):
        return self._num(self.pos_entity, attr=self.pos_attr)

    def _num(self, entity, attr=None):
        raw = self.get_state(entity, attribute=attr) if attr else self.get_state(entity)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
