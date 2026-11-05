# Dyson Pi: a pneumatic button pusher for the Dyson Vis Nav 360

A Raspberry Pi 3 Model B presses the Vis Nav's start button with a pneumatic
cylinder. The Pi switches a relay, the relay switches the solenoid valve, and
the valve fires the cylinder. A second relay's dry contact tells the Pi whether
the robot is on its dock. That closes the loop: the Pi knows whether a press
actually started a cleaning run.

You get:

- **Closed-loop "smart start".** It presses once and watches the sensor. If the
  robot doesn't leave, it assumes the robot was asleep and presses a second
  time. If it still doesn't leave, it stops and raises an alert. It never
  retries blindly.
- **A mobile-first web dashboard** with live updates, schedules, cleaning
  history, an event log, maintenance counters, calibration and hardware tests.
- **A REST API** with token auth, for iOS Shortcuts or Home Assistant.
- **Layered safety:** a hard cap on relay on-time plus a watchdog, relay forced
  OFF on every exit path, a cooldown, a daily limit and an emergency disable
  switch.
- **Survives reboots:** systemd with auto-restart, the hardware watchdog, a
  wait for NTP before schedules run, Wi-Fi auto-reconnect and persistent state.

> [!WARNING]
> **The Raspberry Pi 3 Model B only supports 2.4 GHz Wi-Fi.** A 5 GHz hotspot
> is invisible to it.
> **iPhone:** Settings → Personal Hotspot → turn on **Maximize Compatibility**.
> **Android:** Hotspot settings → AP band → **2.4 GHz**.

---

## Contents

1. [Parts list](#1-parts-list)
2. [Wiring](#2-wiring)
3. [Installation](#3-installation)
4. [Phone hotspot setup](#4-phone-hotspot-setup)
5. [First run and hardware tests](#5-first-run-and-hardware-tests)
6. [Calibration](#6-calibration)
7. [How smart start works](#7-how-smart-start-works)
8. [Scheduling](#8-scheduling)
9. [Safety layers](#9-safety-layers)
10. [Dashboard](#10-dashboard)
11. [REST API](#11-rest-api)
12. [CLI: `dysonctl`](#12-cli-dysonctl)
13. [Logging, backup and restore](#13-logging-backup-and-restore)
14. [Maintenance checklist](#14-maintenance-checklist)
15. [Troubleshooting](#15-troubleshooting)
16. [Development (laptop, no hardware)](#16-development-laptop-no-hardware)
17. [Files and layout](#17-files-and-layout)

---

## 1. Parts list

Electronics and Pi side. The pneumatics (cylinder, valve, air supply and
mounting) are out of scope for this project.

| Qty | Part | Notes |
|---|---|---|
| 1 | Raspberry Pi 3 Model B | Raspberry Pi OS Lite **Bookworm 64-bit** |
| 1 | 5 V / 2.5 A micro-USB supply | The official supply is recommended; undervoltage causes Wi-Fi drops |
| 1 | microSD card, 16 GB or more | A good-quality card (A1/A2) |
| 1 | **Actuator relay module, 3 V DC, with its own power supply** | Driven by GPIO17. It must be a *module* with an input/driver stage (transistor or optocoupler). Never drive a bare relay coil straight from a GPIO pin. |
| 1 | Presence relay (separate) | Its **dry contacts** go to GPIO27 and GND. The contacts must carry no voltage at all. |
| 1 | 2-wire 5 V fan (red/black) | Runs whenever the Pi is on |
| - | Dupont jumper wires (female-to-female) | |
| - | Flyback diode across the valve coil (if it is a DC coil) | Unless the valve or relay board already has one |

## 2. Wiring

> [!CAUTION]
> **GPIO pins are 3.3 V only and not 5 V tolerant.** Never put any external
> voltage on the presence contacts (pins 13/14). They must be *dry* (potential-free)
> relay contacts. Feeding 5 V or 12 V into GPIO27 will destroy the Pi.

### Pin table

Physical pin numbers are on the 40-pin header. GPIO numbers are BCM numbering,
which is what `config.yaml` uses.

| Physical pin | Function | Connects to | Config key |
|---:|---|---|---|
| **4** | 5 V | Fan **red** | - |
| **6** | GND | Fan **black** | - |
| **9** | GND | Actuator relay module **GND** (signal ground) | - |
| **11** | GPIO17 (output) | Actuator relay module **IN** | `hardware.relay_pin: 17` |
| **13** | GPIO27 (input, internal pull-up) | Presence relay **contact 1** | `hardware.presence_pin: 27` |
| **14** | GND | Presence relay **contact 2** | - |

### Diagram

```
                      Raspberry Pi 3 Model B, 40-pin header (pins 1-14 shown)
                       ┌──────────────────────────────┐
                 3V3   │  1 ●  ● 2   5V               │
                 GPIO2 │  3 ●  ● 4   5V  ─────────────┼────────── red   ┐
                 GPIO3 │  5 ●  ● 6   GND ─────────────┼────────── black ┘ FAN (always on)
                 GPIO4 │  7 ●  ● 8   GPIO14           │
     ┌────────── GND   │  9 ●  ● 10  GPIO15           │
     │ ┌──────── GPIO17│ 11 ●  ● 12  GPIO18           │
     │ │  ┌───── GPIO27│ 13 ●  ● 14  GND ─────────┐   │
     │ │  │            └──────────────────────────┼───┘
     │ │  │                                       │
     │ │  │     PRESENCE RELAY (dry contacts,     │
     │ │  │      NO voltage on them!)             │
     │ │  │     ┌───────────────────┐             │
     │ │  └─────┤ contact 1         │             │
     │ │        │      ─o/ o─        │            │
     │ │        │ contact 2         ├─────────────┘
     │ │        └───────────────────┘
     │ │        Closed = robot docked (default; see presence_closed_means_present)
     │ │
     │ │     ACTUATOR RELAY MODULE (3 V DC, own power supply)
     │ │     ┌─────────────────────────────┐
     │ └─────┤ IN                    COM   ├──────── valve supply (+)
     └───────┤ GND                   NO    ├──────── solenoid valve (+)
             │ VCC ◄── own supply +  NC    │ (unused)
             │ GND ◄── own supply −        │         solenoid valve (−) ─── valve supply (−)
             └─────────────────────────────┘
       Use NO (normally open): if the Pi is off or crashes, the valve stays closed.
```

### Grounding: two cases

**A) Non-isolated module (most cheap boards, and single-supply 3 V modules).**
The Pi's GPIO signal is measured against the module's GND, so the grounds must
be shared. Connect module **GND → Pi pin 9**, and connect the relay supply's
negative to that same GND. Without a common ground, the IN signal has no
reference and the relay may click randomly or not at all.

**B) Opto-isolated module with a JD-VCC jumper.**
Remove the JD-VCC jumper. Then:
- input side: **VCC** = 3.3 V (Pi pin 1), **IN** = pin 11, **GND** = pin 9;
- coil side: **JD-VCC** and its GND = the relay's own supply.

The coil supply and the Pi stay fully isolated; only the optocoupler LED is
driven from the Pi. Do **not** connect the coil-side GND to the Pi in this case.

### Polarity

Most modules are **active-low**: IN pulled LOW turns the relay ON. That is the
default (`relay_active_low: true`). If yours is active-high, set it to `false`
and re-run `install.sh`, which also updates the power-on pin state.

The installer adds `gpio=17=op,dh` to `/boot/firmware/config.txt` (`dl` for
active-high), so the firmware holds the relay **OFF from the moment the Pi powers
up**. Without it, GPIO17's default pull-down could energize an active-low relay
during boot.

### Presence sensor

GPIO27 uses the Pi's internal pull-up, so a closed contact reads LOW. By default
**closed = robot present** (`presence_closed_means_present: true`). If your
presence relay opens when the robot docks, set it to `false`.

## 3. Installation

On a fresh Raspberry Pi OS Lite (Bookworm, 64-bit) with SSH enabled (set it up
in Raspberry Pi Imager):

```bash
sudo apt-get install -y git
sudo git clone <your-repo-url> /opt/dyson-pi        # or copy the folder there
sudo bash /opt/dyson-pi/deploy/install.sh
sudo reboot                                         # applies watchdog + GPIO boot states
```

The installer is **idempotent**: re-run it any time, for example after
changing the relay polarity or pins. It:

1. installs apt packages (`python3-gpiozero`, `python3-lgpio`, avahi,
   NetworkManager, `pinctrl`);
2. creates the system user `dyson-pi`, a member of `gpio`;
3. installs the app into a venv at `/opt/dyson-pi/.venv` (with system site
   packages, so apt's lgpio is used);
4. creates `/etc/dyson-pi/config.yaml` from [config.example.yaml](config.example.yaml)
   if it doesn't exist yet;
5. **prompts for the dashboard password** (scrypt-hashed in the database);
6. sets the timezone from the config and enables NTP (systemd-timesyncd);
7. **prompts for the hotspot SSID, password and Wi-Fi country** and creates the
   NetworkManager connection (see section 4);
8. sets the hostname to `dyson-pi` and installs the avahi service, so the
   dashboard is at **http://dyson-pi.local/**;
9. adds `dtparam=watchdog=on` and the GPIO power-on states to `config.txt`, and
   sets systemd's `RuntimeWatchdogSec=15` (a hung Pi reboots itself);
10. installs and enables `dyson-pi.service` and `dyson-pi-netwatch.timer`, starts
    them and checks `/api/health`.

For a non-interactive install, set environment variables:
`DYSON_PASSWORD`, `DYSON_WIFI_SSID`, `DYSON_WIFI_PSK`, `DYSON_WIFI_COUNTRY`,
`DYSON_HOSTNAME`, `DYSON_SKIP_WIFI=1`.

**Update:** `sudo /opt/dyson-pi/deploy/update.sh` runs git pull, reinstalls,
updates the units, restarts and checks health.
**Uninstall:** `sudo /opt/dyson-pi/deploy/uninstall.sh` keeps config and data;
add `--purge` to delete everything.

## 4. Phone hotspot setup

> [!IMPORTANT]
> **2.4 GHz only.** On iPhone, turn on *Maximize Compatibility*. On Android,
> set the hotspot band to *2.4 GHz*.

`install.sh` creates the connection with `nmcli`:

| Setting | Value | Why |
|---|---|---|
| `connection.autoconnect` | `yes` | connect whenever the hotspot is in range |
| `connection.autoconnect-priority` | `100` | preferred over other known networks |
| `connection.autoconnect-retries` | `0` | **retry forever** (NetworkManager's default gives up after 4 tries) |
| `802-11-wireless.band` | `bg` | 2.4 GHz only (all the Pi 3 can do) |
| `802-11-wireless.powersave` | `2` (off) | Wi-Fi power saving is a common cause of Pi drop-outs |

To change the SSID or password later, run
`sudo bash /opt/dyson-pi/deploy/install.sh --reconfigure-wifi`.

**Connectivity watchdog:** `dyson-pi-netwatch.timer` runs every minute. The
network counts as up when the hotspot connection is active **and** the gateway
(your phone) answers a ping. If it has been down for `network.watchdog_down_minutes`
(default 3), the watchdog runs `nmcli connection up`. If that doesn't help, the
next attempt toggles the Wi-Fi radio, and it keeps alternating. Drops, reconnect
attempts and restores go to the event log and journald
(`journalctl -u dyson-pi-netwatch`).

**IP address:** shown on the dashboard (Home → IP, Tools → System) and logged
on every service start. On an iPhone hotspot it is usually `172.20.10.x`.

**iPhone tips:** keep the Personal Hotspot screen open the first time the Pi
connects. The iPhone only advertises its hotspot while that screen is open or
while a device is connected.

## 5. First run and hardware tests

Before connecting air, do these in order.

1. **Relay click:** dashboard → Tools → *Click relay*, or `sudo dysonctl test relay`.
   You should hear one short click (0.15 s). If the relay is ON all the time and
   turns OFF on the click, the polarity is inverted: set
   `relay_active_low: false` and re-run `install.sh`.
2. **Sensor:** Tools → *Watch sensor 30 s*, or `sudo dysonctl test sensor`.
   Lift the robot off the dock and put it back. You should see *raw* change
   immediately, then *robot state* change after about 3 s (the stability window).
3. **Dry run:** Tools → *Smart sequence (dry run)*, or
   `sudo dysonctl test press --mode smart --dry-run`. The full logic runs and is
   logged, but the relay is never energized.
4. Connect air, then run **Single press** with the robot awake. The button must
   be pressed firmly; tune the timing in section 6.
5. Run a full **Smart start**.

`--direct` makes `dysonctl` drive the GPIO itself. Use it only with the service
stopped (`sudo systemctl stop dyson-pi`).

## 6. Calibration

Dashboard → **Tools → Calibration**. Changes apply immediately, are stored in
the database (not in `config.yaml`, so your comments survive), and are marked
**custom** in the dashboard. *Reset* returns to the `config.yaml` values. *Test now*
runs a single press with the current values.

| Setting | Default | Tune it when |
|---|---|---|
| `press_duration_s` | 1.0 s | The button press doesn't register (increase it), or the robot treats it as a long press (decrease it). Capped by `safety.hard_max_on_s` (3 s). |
| `inter_press_delay_s` | 2.5 s | Double press: the robot needs more time after waking before it accepts the start press. |
| `departure_timeout_s` | 20 s | The robot needs longer than this after a press to physically leave the dock. Must be more than 1 s longer than debounce + stability. |
| `max_presses` | 2 | Normally 2 (wake + start). |
| `debounce_ms` | 200 ms | Contact bounce: the raw state jitters when the robot settles. |
| `stability_s` | 3 s | Charge contacts flicker while docking or undocking. Higher values make detection slower but steadier. |

Tip: watch the sensor test while docking the robot by hand. If *settling* flips
back and forth for more than about 2 s, raise `stability_s`.

## 7. How smart start works

```
 robot docked? ──no──► REFUSE (robot is probably cleaning; a press could pause it)
      │                 dashboard shows a warning; "Force press" overrides
     yes
      ▼
   press #1 ──► wait up to departure_timeout_s for the sensor to show "away"
      │                                   │
      │ left ◄────────────────────────────┘ → SUCCESS (logged, last_interaction updated,
      │                                       cleaning run started)
      ▼ still docked → it was asleep; the first press only woke it
   press #2 ──► wait again
      │ left → SUCCESS
      ▼ still docked after max_presses
   FAILED → red alert on the dashboard. No more presses.
```

- **The sensor decides.** The time-based sleep prediction (`last_interaction` vs
  `sleep_timeout_min`) is only shown as a hint ("probably asleep").
- If the robot *starts* leaving right at the timeout, the controller waits for
  the stability window to finish instead of pressing a departing robot, which
  would pause it.
- Every press, departure and return counts as activity (`last_interaction`).
- Departure → return is recorded as a **cleaning run** (start, end, duration,
  and what triggered it). If the robot left or returned while the service was
  down, the run is reconciled at startup and marked *approx.*
- Modes: **Smart** (closed loop), **Single**, **Double** (wake + start,
  `inter_press_delay_s` apart), **Force** (single press that ignores the sensor;
  never schedulable). Single and Double also refuse when the robot is not docked.

## 8. Scheduling

Dashboard → **Schedules**: name, days of week, time and action (smart start by
default). Schedules can be enabled or disabled individually, and each one shows
its next run time. They are stored in SQLite.

- Times are in `scheduler.timezone` (default **Europe/Budapest**). DST is
  handled. A time that doesn't exist on the spring-forward day (02:30) runs at
  03:30. A time that happens twice on the fall-back day runs once.
- **No RTC:** after boot, schedules stay paused until NTP reports sync. The
  dashboard shows "Waiting for NTP".
- **Missed runs are skipped, never fired late.** That covers runs missed while
  the Pi was off, while NTP was unsynced, or delayed by more than
  `misfire_grace_s`. Each one is logged as `schedule.missed`.
- Scheduled runs use the same closed loop and guards. If the robot isn't
  docked, the run is skipped and the reason is logged (`schedule.skipped`).

## 9. Safety layers

| Layer | What it does |
|---|---|
| Hard max on-time | Every pulse is capped at `safety.hard_max_on_s` (3 s), whatever duration is requested. Config validation rejects `press_duration_s` > max. |
| Relay watchdog | A separate supervisor thread forces the relay OFF if it has been on longer than the max + 0.25 s, logs `relay.watchdog` and shows a fault. |
| Forced OFF | On service start, on stop, via `atexit`, in SIGTERM/SIGINT handlers, in a `finally` after every pulse, and after any sequence error. |
| `ExecStopPost` | `deploy/relay-off.sh` latches the pin OFF with `pinctrl` after **any** exit, including `kill -9` and crashes. It also runs as `ExecStartPre`. |
| Power-on state | `gpio=17=op,dh` in `config.txt`: OFF from power-on until the service starts. |
| NO contact | The valve is wired to NO: no power to the Pi or relay means the valve stays closed. |
| One at a time | A lock allows only one press sequence. A concurrent request returns `blocked_busy`. |
| Cooldown | `cooldown_s` (5 s) between sequences. |
| Daily limit | `daily_press_limit` (20) relay activations per local day, test clicks included. Re-checked before every press, even mid-sequence. |
| Disable toggle | Dashboard / `dysonctl disable` / API. Blocks every press, including Force and tests. Persisted across restarts. |
| Sensor faults | Flapping (6 or more changes in 60 s), long stale periods (72 h), and read errors are shown as warnings. Presence *unknown* (not yet settled) blocks presses like *away* does. |
| systemd | `Restart=always`, `WatchdogSec=30` (a hung app is restarted), hardware watchdog (a hung Pi reboots). |

## 10. Dashboard

Open **http://dyson-pi.local/** (or the IP) and log in. It is built for phones:
add it to the home screen. The tabs:

- **Home:**
  - robot status (Docked / Away cleaning / Unknown) with time in the current state;
  - sleep hint;
  - big action buttons;
  - last start attempt;
  - relay state, CPU temperature, presses today, IP;
  - next schedules;
  - the emergency **Disable actuator** switch.

  Alerts (failed start, sensor faults, disabled, waiting for NTP, maintenance
  due) appear as banners at the top.
- **Schedules:** create, edit, enable/disable and delete.
- **History:** cleaning runs, press attempts, and the full event log.
- **Service:** maintenance counters and reminders with *Mark serviced*, plus the
  service log.
- **Tools:** hardware tests, calibration, API tokens, password change, backup
  download, system info.

Live updates use Server-Sent Events. The green dot means connected.

## 11. REST API

Base URL: `http://dyson-pi.local`. Interactive docs are at `/api/docs`.

**Auth:** either the dashboard session cookie, or an API token:
```
Authorization: Bearer dp_xxxxxxxx
```
Create a token in Tools → API tokens or with `sudo dysonctl token create <name>`.
Tokens are shown once and stored hashed. Cookie-authenticated `POST`/`PATCH`/`DELETE`
requests also need the header `X-Requested-With: dyson-pi` (CSRF protection).
Token requests don't.

| Method & path | Description |
|---|---|
| `GET /api/health` | **No auth.** `{"status":"ok", "checks": {...}}`; returns 503 if the DB is broken |
| `GET /api/status` | Full snapshot: robot, relay, actuator, last attempt, alert, schedules, maintenance, system |
| `POST /api/press?mode=smart` | Run a sequence. `mode` = `smart`, `single`, `double` or `force`. `wait=true` blocks until done (up to about 45 s). `dry_run=true` never energizes the relay. **200** = finished (`outcome` is `success`, `completed` or `failed`), **202** = started (`wait=false`), **409** = blocked or refused (`outcome` is `refused_not_present`, `blocked_cooldown`, `blocked_busy`, `blocked_daily_limit` or `blocked_disabled`; see `message`) |
| `POST /api/test/relay?pulse_s=0.15` | Short relay click |
| `POST /api/actuator/disable` | `{"disabled": true}` turns on the emergency disable, `false` re-enables |
| `POST /api/alert/dismiss` | Clear the failed-start alert |
| `GET /api/attempts?limit=50` | Press attempt history |
| `GET /api/runs?limit=50` | Cleaning run history |
| `GET /api/events?limit=100&before=<id>&kind=press` | Event log, newest first |
| `GET /api/stream` | SSE: `status` and `event` messages |
| `GET /api/schedules` | List with `next_run` |
| `POST /api/schedules` | `{"name":"Morning","days":["mon","wed"],"time":"09:30","mode":"smart","enabled":true}`. Days are `0`-`6` (Mon=0) or names. |
| `PATCH /api/schedules/{id}` | Any subset of the fields above |
| `DELETE /api/schedules/{id}` | |
| `GET /api/maintenance` | Counters, reminders, service log |
| `POST /api/maintenance/{id}/serviced` | `{"note":"replaced o-ring"}` |
| `GET/PUT/DELETE /api/calibration` | Read, override (`{"values":{"press.press_duration_s":0.8}}`) or reset |
| `GET/POST/DELETE /api/tokens[/{id}]` | Manage API tokens |
| `POST /api/password` | `{"current":"…","new":"…"}` (session only) |
| `GET /api/backup` | ZIP with a DB snapshot, `config.yaml` and calibration overrides |
| `POST /api/login`, `/api/logout` | Dashboard session |

**Examples**

```bash
TOKEN=dp_xxx
curl -s -X POST -H "Authorization: Bearer $TOKEN" "http://dyson-pi.local/api/press?mode=smart&wait=true"
curl -s -H "Authorization: Bearer $TOKEN" http://dyson-pi.local/api/status | jq .robot
```

**iOS Shortcuts:** add a *Get Contents of URL* action with:
- URL `http://dyson-pi.local/api/press?mode=smart&wait=true`
- Method **POST**
- Header `Authorization` = `Bearer dp_xxx`

Then use *Get Dictionary Value* `outcome` / `message` and *Show Notification*.
If `.local` doesn't resolve while the iPhone *is* the hotspot, use the Pi's IP
(`172.20.10.x`).

**Home Assistant** (`configuration.yaml`):

```yaml
rest_command:
  dyson_smart_start:
    url: "http://dyson-pi.local/api/press?mode=smart&wait=true"
    method: post
    headers: { Authorization: "Bearer dp_xxx" }
    timeout: 90
sensor:
  - platform: rest
    name: Dyson robot
    resource: http://dyson-pi.local/api/status
    headers: { Authorization: "Bearer dp_xxx" }
    value_template: "{{ value_json.robot.state }}"
    json_attributes: [robot, last_attempt, actuator]
    scan_interval: 30
```

## 12. CLI: `dysonctl`

The commands talk to the running service, so the same locks and limits apply.
Run them with `sudo`, which is needed to read the local CLI token.

```
dysonctl status [--json]
dysonctl press --mode smart|single|double|force [--dry-run] [--direct]
dysonctl test relay [--pulse 0.15] [--direct]
dysonctl test sensor [--seconds 30] [--direct]
dysonctl test press --mode smart [--dry-run] [--direct]
dysonctl disable | enable              # emergency actuator toggle
dysonctl relay-off                     # disable via API, or drive the pin OFF if the service is down
dysonctl set-password [--stdin]
dysonctl token create NAME | list | revoke ID
dysonctl config check | show | get hardware.relay_pin
dysonctl health
dysonctl net-watchdog                  # what the timer runs
```

`--direct` drives the GPIO from the CLI process. It refuses to run while the
service is up, because only one process can own the pins.

## 13. Logging, backup and restore

- **journald:** `journalctl -u dyson-pi -f` (service),
  `journalctl -u dyson-pi-netwatch` (Wi-Fi watchdog).
- **Log file:** `/var/log/dyson-pi/dyson-pi.log` (JSON lines, rotated 5 × 5 MB).
- **Event history:** the `events` table in SQLite, shown in History. Pruned
  after `event_retention_days` (180).
- **Health:** `GET /api/health` or `dysonctl health`.
- CPU temperature is logged every 15 min (at warning level from 75 °C), and the
  IP is logged on every start.
- **Backup:** Tools → *Download backup* (or `GET /api/backup`) gives a ZIP with a
  consistent `dyson.db` snapshot, `config.yaml` and the calibration overrides.
- **Restore:**
  ```bash
  sudo systemctl stop dyson-pi
  sudo cp dyson.db /var/lib/dyson-pi/dyson.db && sudo chown dyson-pi: /var/lib/dyson-pi/dyson.db
  sudo rm -f /var/lib/dyson-pi/dyson.db-wal /var/lib/dyson-pi/dyson.db-shm
  sudo cp config.yaml /etc/dyson-pi/config.yaml
  sudo systemctl start dyson-pi
  ```

## 14. Maintenance checklist

Counters tracked persistently (Service tab):

- cylinder actuations;
- relay cycles (these include test clicks);
- successful and failed starts;
- cleaning runs;
- fan run hours (Pi uptime, counted once per boot);
- last successful start.

Reminders are configurable in `config.yaml` → `maintenance.reminders`. Each one
counts actuations, relay cycles, fan hours, cleaning runs or days. *Mark serviced*
resets that reminder's count and logs the date and your note.

| When | Check |
|---|---|
| Every 2000 actuations *(reminder)* | Cylinder: rod moves smoothly, seals not leaking, mounting tight, tip aligned with the button center |
| Every 30 days *(reminder)* | Air supply: pressure, hoses, fittings, no leaks or kinks; valve clicks crisply |
| Every 1000 fan hours *(reminder)* | Blow the dust out of the fan and heatsink; check that it still spins |
| Monthly | Dashboard: no sensor-fault warnings; the failed-start rate is low; CPU temperature below 70 °C |
| Monthly | Press the dock sensor by hand and watch Tools → *Watch sensor* |
| After moving the dock | Re-check button alignment and run *Single press*, then *Smart start* |
| Quarterly | Download a backup; `sudo apt update && sudo apt full-upgrade`; `sudo /opt/dyson-pi/deploy/update.sh` |

## 15. Troubleshooting

| Symptom | Check |
|---|---|
| Relay ON at boot or inverted | `relay_active_low` is wrong. Fix it, re-run `install.sh`, reboot. |
| Relay never clicks | Common ground (section 2: case A vs B); module power; `dysonctl test relay --direct` with the service stopped; `pinctrl get 17` |
| Robot state stays *Unknown* | Sensor not wired or not settled. `dysonctl test sensor`, check pins 13/14. `pinctrl get 27` should show `lo` when the contact is closed. |
| Robot shows *Away* while docked | Invert `presence_closed_means_present`. |
| "Sensor flapping" | Loose wire, or a noisy contact while charging. Raise `debounce_ms` or `stability_s`. |
| Smart start always FAILED | Press too short (raise `press_duration_s`); cylinder misses the button; robot needs longer to leave (raise `departure_timeout_s`); low air pressure |
| Smart start always needs 2 presses | Expected if the robot has been idle more than about 30 min (it was asleep) |
| `blocked_daily_limit` | Something is pressing too often; check History. The limit resets at local midnight. |
| Schedules don't run | Dashboard says "Waiting for NTP": the Pi has no internet through the hotspot; `timedatectl status`. Check the schedule is enabled and the timezone is right. |
| Can't find `dyson-pi.local` | Use the IP. On Windows, mDNS may need Bonjour. `systemctl status avahi-daemon`. |
| Pi never joins the hotspot | 2.4 GHz / *Maximize Compatibility*; SSID spelled exactly; `nmcli device wifi list`; `journalctl -u NetworkManager -b`; check the Wi-Fi country: `sudo raspi-config nonint get_wifi_country` |
| Wi-Fi keeps dropping | Undervoltage: `vcgencmd get_throttled` should be `0x0`. Use the official supply. |
| Service won't start | `journalctl -u dyson-pi -e`; `sudo dysonctl config check`; `lgpio` errors mean the user is not in `gpio` (re-run `install.sh`). |
| Forgot the password | `sudo dysonctl set-password` |
| Pi reboots on its own | The hardware watchdog fired: something hung. `journalctl -b -1 -e` shows the previous boot. |

## 16. Development (laptop, no hardware)

The mock backend is selected automatically off-Pi, or with `--mock`.

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                                            # 104 tests, about 20 s, no real sleeping
python -m dyson_pi --mock --port 8080 --dev-password devpass123
# open http://127.0.0.1:8080: the Home tab has a Simulator panel
```

The **Simulator** panel toggles presence by hand, or enables a simulated Vis Nav
that sleeps after 30 min, needs a wake press when asleep, leaves a few seconds
after a start press, and returns after a configurable time.

Tests run the real service graph on a `FakeClock` with a scriptable sensor
(see [tests/conftest.py](tests/conftest.py)). A smart-start test covering
minutes of robot behavior runs in milliseconds.

## 17. Files and layout

```
/opt/dyson-pi/            app (git checkout) + .venv
/etc/dyson-pi/            config.yaml, relay-off.env
/var/lib/dyson-pi/        dyson.db (SQLite), cli.token, netwatch.json
/var/log/dyson-pi/        dyson-pi.log (rotated)
/etc/systemd/system/      dyson-pi.service, dyson-pi-netwatch.{service,timer}
/etc/systemd/system.conf.d/dyson-pi-watchdog.conf
/etc/avahi/services/dyson-pi.service
/boot/firmware/config.txt # BEGIN dyson-pi ... # END dyson-pi
```

Source code:

```
src/dyson_pi/
  app.py            composition root, lifecycle, status snapshot
  config.py         pydantic config + calibration overrides
  clock.py          injectable Clock / FakeClock
  db.py, events.py  SQLite + event bus
  hardware/         gpio (gpiozero+lgpio), mock + simulated robot, SafeRelay, PresenceMonitor
  logic/            actuator (sequences + guards), robot tracker, maintenance
  scheduler/        DST-safe recurrence, store, runner (NTP gate, skip missed)
  web/              FastAPI app, REST API, auth, static dashboard
  cli/              dysonctl, network watchdog
deploy/             install/update/uninstall, relay-off.sh, systemd + avahi units
tests/              pytest suite
```
