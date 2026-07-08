cover_control.py — the ONLY AppDaemon app in this project.
One instance per window (one block per window in apps.yaml). Holds all
state (deadbands, override, watchdog) and calls the pure functions in
cover_geometry.py and cover_ui.py. Publishes three auto-named sensors:

    sensor.<name>_target_position   raw shading % (ignores boundaries)
    sensor.<name>_boundary          why the cover is/ isn't shading
    sensor.<name>_status            control state (+ countdowns)

where <name> is the apps.yaml block key (or an explicit `name:` arg).

  apps.yaml keys
  --------------
  REQUIRED (no default — omitting one stops the app loading):
  
     module / class        cover_control / CoverControl
     cover_entity          the cover to drive
     pos_sensor            current position sensor, 0-100
     master_switch         app-wide enable input_boolean
     override_bool         this window's manual-override input_boolean
     cloud_sensor          cloud coverage sensor, 0-100
     window_azimuth        bearing the window faces, degrees
     window_height         glass height, metres

  OPTIONAL (shown with defaults — omit any line to accept the default):
  
     name                  (self.name)  slug for the three sensors
     sun_depth             0.30   allowed sun penetration onto floor, metres
     elevation_min         25     sun below this -> open
     elevation_max         70     sun above this -> open
     fov_left              70     deg left of centre still shaded
     fov_right             70     deg right of centre still shaded
     position_min          30     position where the window is fully covered
     position_max          100    fully open
     snap_open             80     target >= this -> snap to 100
     min_diff              5      smallest % change worth moving for
     move_cooldown         600    seconds between auto-moves (time deadband)
     override_duration     7200   seconds before override auto-clears
     cloud_threshold       80     cloud % above which it opens fully
     cloud_clear_threshold 65     cloud % below which shading resumes (hysteresis)
     move_start_timeout    10     seconds to wait for movement to begin
     move_timeout          30     seconds max to reach target
     tick_interval         30     seconds between recompute + UI refresh
 =============================================================================
