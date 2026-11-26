# Changelog

Changes made by the 2026–27 team (Henry Smith, Muhammed Wasif). The original
model is by Jack McLean, Max Johnston and Owen Donnelly (2025–26).

## 2026-11-25 — Critical bug fixes (Phase 2)

### Security
- Removed the Solar Guardian app key and secret that were written into
  `solar_simulation.py` as default values. They are now read only from the
  `SG_APP_KEY` / `SG_APP_SECRET` environment variables (GitHub secrets in the
  hourly workflow). Without them the script runs on the fallback SOC.
  The old credentials remain in the git history and must be revoked by the
  account owner.

### Simulation (`solar_simulation.py`)
- **Curtailment is now modelled.** PV serves the load first, then charges the
  battery only as far as it has room; the rest is curtailed. Previously all
  available PV was counted as generated even with a full battery, so `pv_w`
  always equalled `pv_available` and daily `gen_kwh` was overstated.
  - `pv_w` = PV used; `pv_available` = PV possible.
  - New daily field `available_kwh`; the weather icon now uses it.
- **Full battery reads 100% in cold weather.** SOC is tracked as a fraction of
  the cold-derated capacity. Before, a full battery below 10 °C showed ~95%, so
  the dashboard's "battery full" state (SOC ≥ 99%) could not appear in winter.
- **Clyde CP1 and CP2 no longer forced to share one SOC.** Live SOC is read for
  every controller and keyed "Station / Device". Set `solar_guardian_device`
  for CP1 and CP2 to the device names printed in the log. Until then both use
  the station reading and the output marks `soc_source: "api_shared"`.
- **Fallback SOC fixed.** `FALLBACK_SOC = 0.75` (75% of total). The old 0.50
  seeded the battery at its 50% floor — empty — not mid-range as intended.

### Dashboard (`src/`)
- **Plan tab no longer rates every day red.** `classifyDay()` tested
  `daily_gti_kwh < 1.5`, but that field is always `null` (and `null < 1.5` is
  true in JavaScript). Days are now rated from the battery model: green = full
  before 12:00, amber = full later, red = not expected to fill.
- **Full-charge time comes from the battery model only.** The regression
  estimate gave a time even on days the battery never fills; it is kept as
  `regression_full_charge_time` for reference.
- `energyCalc.js` uses available PV (not used PV) when working out whether a
  load can run, since the controller ramps up to serve extra load.
- Operator chart: bars show available PV, bright = used, faint = curtailed.
  The "SOC with scheduled loads" line now draws on curtailed PV first and
  divides by total capacity (it used 80% of total, overstating every drop).

### Housekeeping
- Workflow: removed the duplicate "Build dashboard" step; installs from
  `requirements.txt` and uses `npm ci`.
- `requirements.txt` now lists what the simulation imports.
- Deleted `vite.config.js.save` (leftover junk).
