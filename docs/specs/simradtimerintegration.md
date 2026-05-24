The bridge (`scripts/simrad_timer_sk.py`) reads Simrad/B&G NMEA 2000 CAN frames directly (PGN 130845 SET_TIMER, PGN 130850 START/STOP/RESET/NEAREST_MINUTE), decodes them, and POSTs events to `POST /api/internal/timer-event` on HelmLog, bypassing Signal K entirely. Signal K was initially considered as the transport but its queuing latency on a busy NMEA bus was 8+ seconds; direct HTTP is sub-second. The endpoint is exempt from HelmLog's global auth middleware (`_PUBLIC_PATHS` in `web.py`) and supports optional bearer-token auth via `HELMLOG_TIMER_TOKEN` env var (leave blank on a private boat LAN). The bridge publishes two path values in its POST body: `racing.startTimer.state` (values: "running" | "stopped" | "reset" | "nearest-minute") and `racing.startTimer.duration` (integer seconds, written only on SET). The simrad timer events drive the data on the race-start page. This spec assumes the race-start web page is always open; persistence when the page is closed will be addressed in a future spec. The spec below describes the specific behaviors.

Below is the logic required to integrate the simrad timer events into HelmLog.

The HelmLog Start page includes a toggle button named Instrument Timer. This button defaults to OFF on every startup, ensuring there is only one source of timer truth. WHEN the Instrument Timer toggle is switched to ON all buttons will be disabled. The user may manually toggle it OFF to re-enable manual control but is not expected to toggle it back ON — the system will automatically turn the toggle ON when the first timer-event POST is received. Note: on reboot there will be a brief window where the toggle is OFF until the first event arrives; this is a known limitation to be addressed in a future spec. Pinging the line data will come from SIMRAD at a later spec.

All timer duration calculations will be made using the NMEA UTC time, taken from the CAN hardware frame timestamp (not system clock). The bridge uses `msg.timestamp` from python-can's `can.Message`, which is set at hardware receive time, ensuring the t0_utc calculation is accurate regardless of event-loop or HTTP publish latency.

WHEN a `racing.startTimer.duration` value is received it SHALL set the HelmLog timer duration, which includes the underlying data and race page, to that value. The duration value will persist through multiple sessions and reboots until a new SET command is issued. The duration data will be shown on the Start page as the TIMER value.

WHEN `racing.startTimer.state = "running"` is received, remaining time will be calculated using the CAN hardware timestamp from the event and the stored duration, to minimise latency between the NMEA data being received and the time remaining shown on screen.

WHEN `racing.startTimer.state = "running"` is received, HelmLog SHALL start the timer with the below cases:
START (any case): compute t0_utc = nmea_timestamp + X where X = stored_duration_s if no prior STOP, or X = stopped_remaining_s if resuming after STOP.
START (first time, no prior STOP): X = stored_duration_s (full duration).
START (after STOP): X = stopped_remaining_s, where stopped_remaining_s = t0_utc − stop_nmea_timestamp, computed and persisted to the DB at the moment "stopped" is received.
START (no open race): also create a race named <DATE TIME>.

The HelmLog timer will count down the time remaining and show it on the Start page, which is assumed to be open at all times during racing.

WHEN `racing.startTimer.state = "stopped"` is received, HelmLog SHALL stop the timer, persist stopped_remaining_s until "reset" is received or duration is updated, and finish the current open race (set its end_utc to the NMEA timestamp). If no race is in progress the finish is a no-op.

WHEN `racing.startTimer.state = "reset"` is received, HelmLog SHALL reset the timer to the stored duration. "reset" will not change the running state of the timer — if running it will continue to run, if stopped it will remain stopped.

WHEN `racing.startTimer.state = "nearest-minute"` is received, HelmLog SHALL move the timer to the nearest minute by rounding up or down using the CAN hardware timestamp from the event. "nearest-minute" will not change the running state of the timer — if running it will continue to run, if stopped it will remain stopped.

## Implementation notes

**CAN bus reading**: `can.Notifier` + `can.AsyncBufferedReader` silently drop all messages in Python 3.10+ unless the running loop is passed explicitly. The bridge uses `loop.run_in_executor(None, bus.recv, 1.0)` — blocking recv in the thread pool — which avoids this fragility entirely.

**HTTP transport**: `HelmLogPublisher` uses `httpx.AsyncClient` with a persistent connection. Back-to-back events (e.g. SET immediately followed by START) share the same TCP connection rather than each paying connection-setup overhead.

**Service deployment**: `/etc/systemd/system/simrad-timer.service` is NOT updated by `git pull`. After any change to `scripts/simrad-timer.service`, copy it manually:
```
sudo cp ~/helmlog/scripts/simrad-timer.service /etc/systemd/system/simrad-timer.service
sudo systemctl daemon-reload && sudo systemctl restart simrad-timer
```
