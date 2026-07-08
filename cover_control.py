import hassapi as hass
import datetime
import math

from cover_geometry import compute_target
from cover_ui import boundary_text, status_text

class CoverControl(hass.Hass):

    def initialize(self):
        a = self.args

        # --- Wiring (required) ---
        self.cover_entity  = a["cover_entity"]
        self.pos_sensor    = a["pos_sensor"]
        self.master_switch = a["master_switch"]
        self.override_bool = a["override_bool"]
        self.cloud_sensor  = a["cloud_sensor"]

        # --- Geometry config passed straight to compute_target() ---
        self.cfg = {
            "azimuth":   float(a["window_azimuth"]),
            "height":    float(a["window_height"]),
            "depth":     float(a.get("sun_depth", 0.30)),
            "elev_min":  float(a.get("elevation_min", 25)),
            "elev_max":  float(a.get("elevation_max", 70)),
            "fov_left":  float(a.get("fov_left", 70)),
            "fov_right": float(a.get("fov_right", 70)),
            "pos_min":   float(a.get("position_min", 30)),
            "pos_max":   float(a.get("position_max", 100)),
            "snap_open": float(a.get("snap_open", 80)),
        }

        # --- Control tunables ---
        self.min_diff       = float(a.get("min_diff", 5))
        self.cooldown       = float(a.get("move_cooldown", 600))
        self.override_dur    = float(a.get("override_duration", 7200))
        self.cloud_threshold = float(a.get("cloud_threshold", 80))
        self.cloud_clear     = float(a.get("cloud_clear_threshold", 65))
        if self.cloud_clear >= self.cloud_threshold:
            # A clear point at/above the open point disables the band; keep it
            # sane so cloud can never latch open forever.
            self.cloud_clear = self.cloud_threshold - 1
            self.log("cloud_clear_threshold must be below cloud_threshold; adjusted.")
        self.move_start_to  = float(a.get("move_start_timeout", 10))
        self.move_to        = float(a.get("move_timeout", 30))
        self.tick_interval  = float(a.get("tick_interval", 30))

        # --- Auto-derived sensor names ---
        slug = a.get("name", self.name).lower().replace(" ", "_")
        self.s_target   = f"sensor.{slug}_target_position"
        self.s_boundary = f"sensor.{slug}_boundary"
        self.s_status   = f"sensor.{slug}_status"

        # --- Internal state ---
        now = datetime.datetime.now()
        self.last_move_time  = now - datetime.timedelta(seconds=self.cooldown)
        self.override_start  = now
        self.cloud_open      = False
        self.moving_by_app   = False
        self.commanded_target = None
        self.pos_at_command  = None
        self.retry_done      = False
        self.override_handle = None
        self.start_handle    = None
        self.arrive_handle   = None
        self.cooldown_handle = None

        # --- Subscriptions ---
        self.listen_state(self.on_master,   self.master_switch)
        self.listen_state(self.evaluate,    self.cloud_sensor)
        self.listen_state(self.on_override, self.override_bool)
        self.listen_state(self.on_position, self.pos_sensor)

        # Sun moves continuously, so recompute + refresh on a timer too.
        self.run_every(self.evaluate, now, self.tick_interval)

        self.log(f"CoverControl '{slug}' online -> {self.cover_entity}")
        if self.get_state(self.master_switch) == "on":
            self.last_move_time = now - datetime.timedelta(seconds=self.cooldown)
            self.evaluate()

    # =========================================================================
    #  MAIN EVALUATION  ^`^t publishes all three sensors, moves when clear
    # =========================================================================
    def evaluate(self, *args):
        # --- Compute the two display values first (best effort) ---
        az    = self._num("sun.sun", attr="azimuth")
        el    = self._num("sun.sun", attr="elevation")
        cloud = self._num(self.cloud_sensor)

        if az is None or el is None:
            self._publish_status("error", target="sun data unavailable")
            return

        raw, boundary = compute_target(az, el, self.cfg)

        # Cloud sits above every geometric boundary in priority. Hysteresis:
        if cloud is not None:
            if self.cloud_open:
                if cloud < self.cloud_clear:
                    self.cloud_open = False
            elif cloud > self.cloud_threshold:
                self.cloud_open = True
        if self.cloud_open:
            boundary = "cloud"

        effective = self.cfg["pos_max"] if boundary != "clear" else raw

        # target_position and boundary sensors update regardless of control state.
        self.set_state(self.s_target, state=raw,
                       attributes={"unit_of_measurement": "%",
                                   "icon": "mdi:window-shutter"})
        self.set_state(self.s_boundary, state=boundary_text(boundary),
                       attributes={"reason": boundary, "icon": "mdi:sun-angle"})

        # --- Now the status sensor + movement decisions ---
        if self.get_state(self.master_switch) == "off":
            self._publish_status("master_off")
            return

        if self.get_state(self.override_bool) == "on":
            self._publish_status("override", minutes=self._mins(
                self.override_start, self.override_dur))
            return

        current = self._num(self.pos_sensor)
        if current is None:
            self._publish_status("error", target="position unavailable")
            return

        if self.moving_by_app:
            self._publish_status("moving", target=self.commanded_target)
            return

        diff = abs(effective - current)
        if diff < 1:
            self._publish_status("stable")
            return
        if diff < self.min_diff:
            self._publish_status("position_deadband")
            return

        elapsed = (datetime.datetime.now() - self.last_move_time).total_seconds()
        if elapsed < self.cooldown:
            remaining = self.cooldown - elapsed
            self._publish_status("time_deadband", minutes=math.ceil(remaining / 60))
            # Re-evaluate exactly when the cooldown ends (one-shot).
            if self.cooldown_handle is None:
                self.cooldown_handle = self.run_in(self._cooldown_over, remaining + 1)
            return

        self._issue_move(effective)

    def _cooldown_over(self, kwargs):
        self.cooldown_handle = None
        self.evaluate()

    # =========================================================================
    #  MOVEMENT + WATCHDOGS
    # =========================================================================
    def _issue_move(self, target):
        self.pos_at_command = self._num(self.pos_sensor)
        if self.pos_at_command is not None and abs(target - self.pos_at_command) < 1:
            self._publish_status("stable")
            return

        self._cancel_watchdogs()
        self.commanded_target = round(target)
        self.moving_by_app    = True
        self.retry_done       = False
        self.last_move_time   = datetime.datetime.now()

        self._publish_status("moving", target=self.commanded_target)
        self.call_service("cover/set_cover_position",
                          entity_id=self.cover_entity,
                          position=self.commanded_target)
        self.start_handle = self.run_in(self._check_started, self.move_start_to)

    def _check_started(self, kwargs):
        self.start_handle = None
        if not self.moving_by_app:
            return
        cur = self._num(self.pos_sensor)
        if cur is None:
            return
        moved = self.pos_at_command is None or abs(cur - self.pos_at_command) >= 1
        if moved:
            self.arrive_handle = self.run_in(self._check_arrived, self.move_to)
        elif not self.retry_done:
            self.retry_done = True
            self._publish_status("moving", target=self.commanded_target)
            self.call_service("cover/set_cover_position",
                              entity_id=self.cover_entity,
                              position=self.commanded_target)
            self.start_handle = self.run_in(self._check_started, self.move_start_to)
        else:
            self.moving_by_app = False
            self.commanded_target = None
            self._publish_status("error", target="no response")

    def _check_arrived(self, kwargs):
        self.arrive_handle = None
        if not self.moving_by_app:
            return
        cur = self._num(self.pos_sensor)
        self.moving_by_app = False
        target = self.commanded_target
        self.commanded_target = None
        if cur is not None and abs(cur - target) < self.min_diff:
            self.evaluate()                      # arrived -> recompute -> stable
        else:
            self._activate_override()            # stuck mid-move -> hand to human

    # =========================================================================
    #  MANUAL MOVE DETECTION
    # =========================================================================
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
            return  # floating-point jitter, not a real move

        if self.moving_by_app:
            # Our own move  ^`^t resolve arrival in real time.
            if abs(new_f - self.commanded_target) < self.min_diff:
                self._cancel_watchdogs()
                self.moving_by_app = False
                self.commanded_target = None
                self.evaluate()
        else:
            # Nobody commanded this -> a human did -> override.
            self._activate_override()

    # =========================================================================
    #  OVERRIDE + MASTER
    # =========================================================================
    def _activate_override(self):
        self.turn_on(self.override_bool)   # rest is handled by on_override

    def on_override(self, entity, attribute, old, new, kwargs):
        if new == "on":
            if self.override_handle:
                self.cancel_timer(self.override_handle)
            self.override_start = datetime.datetime.now()
            self.override_handle = self.run_in(self._clear_override, self.override_dur)
            self.evaluate()
        else:
            if self.override_handle:
                self.cancel_timer(self.override_handle)
                self.override_handle = None
            self.evaluate()

    def _clear_override(self, kwargs):
        self.override_handle = None
        self.turn_off(self.override_bool)  # fires on_override("off") -> evaluate

    def on_master(self, entity, attribute, old, new, kwargs):
        if new == "on":
            # Force an immediate sync by clearing the cooldown once.
            self.last_move_time = datetime.datetime.now() - datetime.timedelta(
                seconds=self.cooldown)
            self.evaluate()
        else:
            self._cancel_watchdogs()
            self.moving_by_app = False
            self.commanded_target = None
            self._publish_status("master_off")

    # =========================================================================
    #  HELPERS
    # =========================================================================
    def _publish_status(self, kind, minutes=None, target=None):
        self.set_state(self.s_status,
                       state=status_text(kind, minutes=minutes, target=target),
                       attributes={"kind": kind,
                                   "icon": "mdi:robot-confused" if kind == "error"
                                   else "mdi:robot"})

    def _mins(self, start_time, duration):
        elapsed = (datetime.datetime.now() - start_time).total_seconds()
        return math.ceil(max(0, duration - elapsed) / 60)

    def _cancel_watchdogs(self):
        for h in ("start_handle", "arrive_handle"):
            handle = getattr(self, h)
            if handle:
                self.cancel_timer(handle)
                setattr(self, h, None)

    def _num(self, entity, attr=None):
        raw = self.get_state(entity, attribute=attr) if attr else self.get_state(entity)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
