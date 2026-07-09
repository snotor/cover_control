# Adaptive Cover Shading

Sun-aware shading for Home Assistant covers, running as a single [AppDaemon](https://appdaemon.readthedocs.io/) app.

It positions each cover from the live sun position so direct sunlight reaches no more than a set distance into the room — keeping rooms cooler in summer without leaving them dark. It opens fully when the sun is too low, too high, off to the side, or when it's cloudy, and backs off if you move the cover by hand.

## Requirements

- Home Assistant (uses the built-in `sun.sun`).
- AppDaemon ([add-on](https://github.com/hassio-addons/addon-appdaemon) or container).
- A cover that supports `set_cover_position` (`0 = closed`, `100 = open`).
- A cloud-coverage sensor with a `0–100` state.
- Two `input_boolean` helpers: an app-wide enable and one override toggle per window.

## Install

1. Copy `cover_control.py` into your `appdaemon/apps/` folder.
2. Create the helpers:

   ```yaml
   input_boolean:
     covers_enabled:
       name: Covers enabled
     livingroom_override:
       name: Living room override
   ```

3. Add one block per window to `apps.yaml`:

   ```yaml
   livingroom:
     module: cover_control
     class: CoverControl
     cover_entity: cover.livingroom
     master_switch: input_boolean.covers_enabled
     override_bool: input_boolean.livingroom_override
     cloud_sensor: sensor.cloud_coverage
     window_azimuth: 145      # bearing the window faces (0=N, 90=E, 180=S, 270=W)
     window_height: 1.0       # glass height in metres
   ```

4. Reload AppDaemon.

Only these keys are required; every other option (penetration depth, elevation and field-of-view limits, deadbands, timeouts, cloud thresholds) has a default and is documented at the top of `cover_control.py`.

## What you get

Each window publishes three sensors, named from its block key:

- `sensor.<name>_target_position` — the position the sun geometry wants.
- `sensor.<name>_boundary` — why it is or isn't shading (shading / cloudy / sun too low / etc.).
- `sensor.<name>_status` — control state (stable, override, deadband, moving…).

Toggle `covers_enabled` to enable, and each window's override boolean to pause it manually.

## Optional

- **Inverted cover** (`0 = open`): set `position_min: 100` and `position_max: 0`.
- **Preview before installing**: edit the `CONFIG` block in `simulate_day.py` and run `python3 simulate_day.py` to print a day's shading curve — no Home Assistant needed.

## License

[MIT](LICENSE)
