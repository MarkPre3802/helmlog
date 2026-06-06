## Technical integration

The bridge (`scripts/simrad_timer_sk.py`) reads Simrad/B&G NMEA 2000 CAN frames directly (PGN 130845 SET_TIMER, PGN 130850 START/STOP/RESET/NEAREST_MINUTE), decodes them, and POSTs events to `POST /api/internal/timer-event` on HelmLog, bypassing Signal K entirely. Signal K was initially considered as the transport but its queuing latency on a busy NMEA bus was 8+ seconds; direct HTTP is sub-second. The endpoint is exempt from HelmLog's global auth middleware (`_PUBLIC_PATHS` in `web.py`) and supports optional bearer-token auth via `HELMLOG_TIMER_TOKEN` env var (leave blank on a private boat LAN). The bridge publishes two path values in its POST body: `racing.startTimer.state` (values: "running" | "stopped" | "reset" | "nearest-minute") and `racing.startTimer.duration` (integer seconds, written only on SET).

HelmLog can also write to B&G instruments via `can_writer.py` (`CANWriter`), which claims NMEA 2000 source address `0x7E` on startup and sends PGN 130845 (SET duration) and PGN 130850 (START/STOP/RESET/NEAREST-MINUTE) using Fast Packet encoding. The `CANWriter` instance is created in `main.py` and injected into `app.state.can_writer`; route handlers access it from there.

## Instrument Timer toggle

The Start page has an **Instrument Timer** toggle with two states:

**OFF (default on startup):** HelmLog operates standalone. Inbound B&G timer events are stored but do not update the UI clock or timer state. No outbound commands are sent to B&G instruments. All HelmLog UI buttons function normally.

**ON:** Full bi-directional sync.
- Inbound B&G events drive the HelmLog timer (clock, duration, running/stopped state).
- HelmLog UI actions also command B&G instruments automatically (see HelmLog → B&G section below).
- All HelmLog UI buttons remain **enabled** — the user can act from either side.
- The B&G control panel buttons (Start / Stop / Reset / Nearest min) send direct commands to instruments.

**Auto-ON:** The toggle switches ON automatically when the first `POST /api/internal/timer-event` is received from the bridge. On reboot there is a brief window where the toggle is OFF until the first B&G event arrives; this is a known limitation to be addressed in a future spec.

The toggle state does **not** persist through reboots (always starts OFF). Duration and stopped_remaining_s do persist.

## Headless mode

HelmLog supports fully headless operation driven by B&G instruments — the race-start web page does not need to be open.

The bridge (`simrad_timer_sk.py`) and `POST /api/internal/timer-event` are server-side components. All inbound event processing — timer state updates, race creation, and race end — happens in the database regardless of whether any browser has the Start page open. A helm can run an entire race using only B&G instruments with no interaction with HelmLog:

- B&G SET → duration stored in DB
- B&G START → race created (if none open), `t0_utc` computed and stored
- B&G STOP → race ended, `stopped_remaining_s` stored
- B&G RESET / NEAREST-MINUTE → timer state updated in DB

**Toggle in headless:** The toggle defaults OFF on process start. The first inbound B&G event auto-flips it ON server-side — no UI action required. Subsequent events are processed normally.

**UI reconnect:** When the Start page is opened during or after headless operation, it reads persisted state from the DB and displays the current timer and race. If the HelmLog process restarted since the last B&G event, the toggle will be OFF until the next event arrives; the stored duration and race data are still present.

**Outbound commands are UI-only:** HelmLog → B&G commands (FSM auto-coupling and B&G panel buttons) require a user action on the Start page. There is no headless outbound path — in pure headless mode B&G instruments are the sole source of commands.

## B&G → HelmLog (inbound events)

All calculations use the NMEA UTC time taken from the CAN hardware frame timestamp (not system clock). The bridge uses `msg.timestamp` from python-can's `can.Message`, set at hardware receive time.

WHEN a `racing.startTimer.duration` value is received it SHALL set the HelmLog timer duration and persist it until a new SET command is issued. The duration is shown on the Start page as the TIMER value.

WHEN `racing.startTimer.state = "running"` is received:
- Compute `t0_utc = nmea_timestamp + X` where:
  - X = `stored_duration_s` if no prior STOP (fresh start)
  - X = `stopped_remaining_s` if resuming after a STOP
- If no race is open, create one named `<DATE TIME>` using the NMEA timestamp.

WHEN `racing.startTimer.state = "stopped"` is received, HelmLog SHALL stop the timer, persist `stopped_remaining_s = t0_utc − nmea_timestamp` (clamped to 0), and end the current open race (set `end_utc` to the NMEA timestamp). If no race is in progress the finish is a no-op.

WHEN `racing.startTimer.state = "reset"` is received, HelmLog SHALL reset the timer to the stored duration without changing running state — if running it continues to run, if stopped it remains stopped.

WHEN `racing.startTimer.state = "nearest-minute"` is received, HelmLog SHALL move the timer to the nearest whole minute using the NMEA timestamp, without changing running state.

## HelmLog → B&G (outbound commands)

When the Instrument Timer toggle is **ON** and a CAN writer is available, the following HelmLog UI actions automatically command B&G instruments:

| HelmLog action | B&G command |
|---|---|
| ARM 5-4-1-0 | SET duration = 5 min (PGN 130845) |
| +1 Minute | SET duration = stored_duration_s + 60 s |
| −1 Minute | SET duration = stored_duration_s − 60 s |
| Sync to Gun | nearest-minute (PGN 130850) |
| Reset | reset (PGN 130850) |
| Postpone | stop (PGN 130850) |
| General Recall | stop (PGN 130850) |
| Abandon | stop (PGN 130850) |

When the toggle is OFF no outbound commands are sent from FSM actions.

If the CAN writer is unavailable (no CAN hardware, failed to open) the outbound step is silently skipped — HelmLog continues to function as standalone.

## B&G control panel buttons

The Start page includes explicit B&G instrument control buttons (always visible when toggle is ON, disabled otherwise):

| Button | Command sent |
|---|---|
| Start | start (PGN 130850) |
| Stop | stop (PGN 130850) |
| Reset | reset (PGN 130850) |
| Nearest min | nearest-minute (PGN 130850) |

These send directly to instruments via `POST /api/race-start/bg-timer-command`. They are independent of the FSM auto-coupling above — the user can trigger them at any time for direct manual control.

## Feedback loop

When HelmLog sends a command to B&G (e.g. STOP from Postpone), the bridge will receive that CAN frame and echo it back as an inbound timer-event POST. All inbound handlers (`handle_stopped`, `end_race`, etc.) are idempotent so the echo is a harmless no-op.

## Implementation notes

**CAN bus reading**: `can.Notifier` + `can.AsyncBufferedReader` silently drop all messages in Python 3.10+ unless the running loop is passed explicitly. The bridge uses `loop.run_in_executor(None, bus.recv, 1.0)` — blocking recv in the thread pool — which avoids this fragility entirely.

**HTTP transport**: `HelmLogPublisher` uses `httpx.AsyncClient` with a persistent connection. Back-to-back events (e.g. SET immediately followed by START) share the same TCP connection rather than each paying connection-setup overhead.

**Service deployment**: `/etc/systemd/system/simrad-timer.service` is NOT updated by `git pull`. After any change to `scripts/simrad-timer.service`, copy it manually:
```
sudo cp ~/helmlog/scripts/simrad-timer.service /etc/systemd/system/simrad-timer.service
sudo systemctl daemon-reload && sudo systemctl restart simrad-timer
```
