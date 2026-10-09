# Tray startup and connection feedback

Windows login entries now launch `LuxEdge.exe --start-in-tray`. Enabled legacy
entries are migrated on packaged startup; disabled entries remain disabled.
Normal launches and tray actions show the control panel. Automatic secondary
launches do not show it. If creating the tray fails, the window remains accessible.

## Connection contract

`GET /api/status` retains `connection` and adds `connectivity`:

```json
{
  "state": "connected",
  "ip": "192.0.2.1",
  "port": 7777,
  "udp_ok": true,
  "http_ok": null,
  "checked_at": 1791580000.0,
  "consecutive_failures": 0
}
```

`checked_at` is Unix time of the latest accepted completed probe. `null` protocol
values mean untested. A UDP PONG is sufficient; HTTP is the automatic fallback.
The manual test probes both protocols and uses the same state tracker. Its
`applied` field is false when the result was superseded or the target stopped.

| State | Presentation |
| --- | --- |
| `connected` | Green, UDP verified |
| `http_only` | Yellow, HTTP reachable / UDP unverified |
| `checking` | Blue, awaiting a conclusive result |
| `disconnected` | Red, three consecutive failed probes |
| `unconfigured` | Gray, no IP |
| `stopped` | Gray, worker stopped |
| `backend_unavailable` | Red, Electron cannot reach the backend |

One successful probe recovers immediately. The previous confirmed state is
retained for the first two consecutive failures; the latest probe fields still
record those failures. Automatic checks wait three seconds between probes, with
1-second UDP and 1.5-second HTTP timeouts. FPS and sent-packet counts never prove
device reachability. Target generations and probe ordering reject obsolete
results after IP/port changes, stop/restart, and overlapping manual checks.

## Verification

```powershell
node --test tests/*.test.js
python -m unittest discover -s tests -p 'test_*.py'
node scripts/verify-asar.js
python scripts/verify-package.py
```

`scripts/test-packaged.cjs` additionally exercises the built Windows application
using Playwright Electron, a temporary profile, and a local UDP PONG peer. It
checks hidden startup, automatic/manual secondary launches, positive FPS without
a device, reconnect, manual testing, and disconnect. It writes screenshots and a
result file under `build/packaged-smoke-*`.

Set `PLAYWRIGHT_MODULE_PATH` if Playwright comes from an external runtime. The
smoke test uses backend port 18888 through `LUXEDGE_BACKEND_PORT`; normal operation
continues to use 8888. This keeps testing separate from an installed running app.
The test process uses `--disable-gpu --in-process-gpu --no-sandbox` because the
automation environment could not start the normal Chromium GPU/renderer helpers.
These flags are **not** added to the application or Windows login entry. External
font requests and dot transitions are disabled only in the test for deterministic
loading and final-color capture.

Windows sign-out/reboot and default GPU startup are separate manual checks.
Physical LED reception is separate from UDP/HTTP reachability.

### Verified on 2026-10-10

- 23 Node tests and 42 Python tests passed.
- The packaged smoke test passed; screenshots and `result.json` are in
  `build/packaged-smoke-HmwBDJ`. Both green and red presentations were inspected.
- With no UDP peer, capture still ran at about 59 FPS while the connection was red.
  Adding a PONG peer made it green; removing it returned it to red after three
  failed probes. Manual testing retained the correct main indicator.
- Source/bytecode and backend hashes matched both the unpacked package and the
  files extracted from the NSIS installer.
- Previous verification used `dist/LuxEdge Setup 1.6.4.exe`. After this version
  bump, verify the new artifact as `dist/LuxEdge Setup 1.6.4.2.exe`.
- Physical device `33.33.33.19:7777` returned PONG and HTTP `/status` reported
  firmware 1.6.4 / 74 LEDs. The repository config's `33.33.33.21` timed out.
  User configuration was not changed by the test. Real Windows login was not tested.
