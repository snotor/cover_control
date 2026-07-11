# Adaptive Cover Shading

Sun-aware shading for Home Assistant covers, running as a single [AppDaemon](https://appdaemon.readthedocs.io/) app.

It positions each cover from the live sun position so direct sunlight reaches no more than a set distance into the room — keeping rooms cooler in summer without leaving them dark. When shading isn't needed (sun too low, too high, off to the side, or — optionally — cloudy) it opens the cover once and then goes **idle**, leaving it alone so you're free to move it yourself. If you move the cover by hand it backs off automatically and hands control to you.

## Requirements

- Home Assistant (uses the built-in `sun.sun`).
- AppDaemon ([add-on](https://github.com/hassio-addons/addon-appdaemon) or container).
- A cover that supports `set_cover_position` (`0 = closed`, `100 = open`).
- Two `input_boolean` helpers: an app-wide enable and one override toggle per window.
- *(Optional)* A cloud-coverage sensor with a `0–100` state, so shading opens when it's overcast (omit `cloud_sensor` to disable this).

## Install

1. In Home Assistant, create one `input_boolean` to enable the app (`covers_enabled`) plus one manual-override `input_boolean` per window (e.g. `livingroom_override`).

2. *(Optional)* Create a cloud-coverage sensor so shading pauses when it's overcast. The easiest way: **Settings → Devices & services → Helpers → Create helper → Template → Sensor**, then set the state to:

  

```
{{ state_attr('weather.forecast_home', 'cloud_coverage') | float(0) }}
```

    Replace `weather.forecast_home` with your weather entity if it's named differently.

3. Copy `cover_control.py` into your `appdaemon/apps/` folder.

4. Add one block per window to `apps.yaml`:

  

```
livingroom:
  module: cover_control
  class: CoverControl
  cover_entity: cover.livingroom
  master_switch: input_boolean.covers_enabled
  override_bool: input_boolean.livingroom_override
  cloud_sensor: sensor.cloud_coverage   # optional; omit to skip the cloud check
  window_azimuth: 145      # bearing the window faces (0=N, 90=E, 180=S, 270=W)
  window_height: 1.0       # glass height in metres
```

5. Reload AppDaemon.

Only these keys are required; every other option (penetration depth, elevation and field-of-view limits, deadbands, timeouts, cloud thresholds) has a default and is documented at the top of `cover_control.py`.

<details>
<summary><strong>Optional settings reference</strong></summary>

All keys go in the window's `apps.yaml` block alongside the required ones.

### Geometry

| Key | Default | Unit | Valid range | Description |
|-----|---------|------|-------------|-------------|
| `sun_depth` | `0.20` | m | `> 0` | How far direct sun is allowed to reach onto the floor. Smaller = cover closes more aggressively. |
| `elevation_min` | `25` | ° | `0–90, < elevation_max` | Sun below this angle → idle (early morning / late afternoon). |
| `elevation_max` | `70` | ° | `0–90, > elevation_min` | Sun above this angle → idle (high summer midday). |
| `fov_left` | `70` | ° | `1–89` | Degrees left of window centre still considered in view. |
| `fov_right` | `70` | ° | `1–89` | Degrees right of window centre still considered in view. |

### Cover position

| Key | Default | Unit | Valid range | Description |
|-----|---------|------|-------------|-------------|
| `position_min` | `30` | % | `0–100` | Cover position that fully blocks the sun. Usually not 0 to avoid motor strain at the hard stop. |
| `position_max` | `100` | % | `0–100` | Fully open position. For an inverted cover (`0 = open`) swap with `position_min`. |
| `snap_open` | `80` | % | `0–100` | If the computed uncovered fraction exceeds this, snap straight to `position_max` instead. |
| `pos_sensor` | — | entity | — | Alternative position source (e.g. a dedicated sensor). Defaults to the cover's own `current_position` attribute. |

### Cloud

| Key | Default | Unit | Valid range | Description |
|-----|---------|------|-------------|-------------|
| `cloud_sensor` | — | entity | — | Cloud coverage sensor (`0–100`). Omit to disable cloud handling entirely. |
| `cloud_threshold` | `80` | % | `0–100, > cloud_clear_threshold` | Cloud coverage above this → stop shading, open, go idle. |
| `cloud_clear_threshold` | `70` | % | `0–100, < cloud_threshold` | Cloud coverage must drop below this before shading resumes (hysteresis to prevent flapping). |

### Movement

| Key | Default | Unit | Valid range | Description |
|-----|---------|------|-------------|-------------|
| `min_diff` | `5` | % | `1–50` | Minimum position change worth commanding. Prevents chasing tiny sun-angle shifts. |
| `move_cooldown` | `600` | s | `≥ 0` | Minimum time between consecutive app-commanded moves. |
| `move_start_timeout` | `10` | s | `> 0` | How long to wait for the cover to start moving before retrying the command once. |
| `move_timeout` | `30` | s | `> 0` | How long to wait for the cover to reach its target before counting the attempt as failed. |
| `tick_interval` | `30` | s | `5–300` | How often the app re-evaluates sun position and updates the sensors. |

### Retry and give-up

| Key | Default | Unit | Valid range | Description |
|-----|---------|------|-------------|-------------|
| `max_retries` | `3` | — | integer, `0` = infinite | Consecutive failed shading moves before giving up and showing `Idle (cover unresponsive)`. |
| `midnight_reset` | `true` | — | `true` / `false` | Clear the give-up latch automatically at midnight each day. Set to `false` to require a manual master toggle or restart. |

### Override

| Key | Default | Unit | Valid range | Description |
|-----|---------|------|-------------|-------------|
| `override_duration` | `7200` | s | `> 0` | How long a manual override stays active before the app resumes control automatically. Default is 2 hours. |

</details>

## What you get

Each window publishes three sensors, named from its block key:

- `sensor.<name>_target_position` — the position the sun geometry wants.
- `sensor.<name>_boundary` — why it is or isn't shading (Shading / Cloudy / Sun too low / etc.).
- `sensor.<name>_status` — control state (stable / moving / idle / override / deadband…).

`covers_enabled` turns the whole app on or off. Switching it back on skips the deadbands and jumps straight to target.

Moving a cover by hand while the sun is on the window arms that window's override and the app backs off. It auto-clears after `override_duration` (default 2 h), or you can turn it off early yourself. Moving the cover at night (sun out of view) doesn't arm the override.

## Features

- **Idle.** When shading isn't needed the app opens the cover once, then leaves it alone.
- **Retry and give-up.** Failed shading moves surface an error and retry each tick. After `max_retries` consecutive failures (default `3`, `0` = forever) it gives up and shows `Idle (cover unresponsive)`.
- **Give-up reset.** Clears automatically at midnight (`midnight_reset: false` to disable). Also resets on master toggle or restart.
- **Inverted cover** (`0 = open`): set `position_min: 100` and `position_max: 0`.

## License

[MIT](https://github.com/snotor/cover_control/blob/main/LICENSE)
