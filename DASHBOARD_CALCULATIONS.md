# Dashboard Calculations — VIP Solar Dashboard

This document covers all the physics and logic that drives the simulation and the browser-side feasibility calculations.

---

## Python Simulation — `solar_simulation.py`

### 1. Solar irradiance — GTI transposition

Global Horizontal Irradiance (GHI) from Open-Meteo is converted to Global Tilted Irradiance (GTI) using:
- Isotropic sky model
- Manual solar geometry (declination, hour angle, zenith angle)
- Bifacial gain where applicable (currently set to 0 for Clyde — derating already absorbs the real contribution)

### 2. PV model

```
GTI
→ NOCT cell temperature correction
→ Temperature derate (γ coefficient)
→ DC power (Pdc)
→ system_derating multiplier
→ MPPT clip (rated controller input limit)
→ Inverter efficiency
→ AC power output (pv_w)
```

**`system_derating`** is a site-specific multiplier applied to `Pdc` before the MPPT clip. It corrects for real-world losses not captured by the panel datasheet. Derived from validation against measured data:

| Site | Derating | Source |
|---|---|---|
| Chryston | 1.0 | Not yet validated |
| Sunny Cycles | 0.85 | Estimated |
| Cumbernauld | 1.0 | Not yet validated |
| Denmilne | 1.0 | Not yet validated |
| Clyde CP1 & CP2 | 0.876 | ERA5 calibration from `validate_clyde.py` |

Applying derating before the MPPT clip is intentional — derating a larger system is physically different from clipping a derated one.

### 3. Battery model — 3-state charge controller

The EPEVER controller operates in three states which the simulation replicates:

**State 1 — Bulk (SOC < 99%)**
Controller outputs maximum MPPT power. Load and battery share PV in parallel. Battery receives whatever PV cannot supply to the load.

**State 2 — Float, load > PV headroom**
Battery is full. Load is connected but exceeds the PV available in float mode. Battery discharges the shortfall. SOC drops.

**State 3 — Float, load ≤ PV headroom ("free energy")**
Battery is full. Load is small enough that the controller ramping from float toward bulk supplies it entirely from PV. No battery cost.

This three-state behaviour was confirmed from real measured data (clydecycle1.xlsx, clydecycle2.xlsx, August 2025 – February 2026).

### 4. SOC convention

EPEVER reports SOC as **0–100% of total battery capacity** (verified from voltage-SOC lookup table cross-reference).

All sites use AGM batteries with 50% depth of discharge. The simulation works in total capacity with a DoD floor at 50%:

```
EPEVER 50%  →  0% usable   (DoD floor — never discharge below this)
EPEVER 100% →  100% usable (fully charged)
```

SOC seeding:
```python
initial_soc_frac = epever_pct / 100.0   # fraction of TOTAL capacity, floor 0.50
```
With no live reading, `FALLBACK_SOC = 0.75` (75% total = half the usable range).
SOC is tracked as a fraction of the battery's cold-derated capacity, so a full
battery reads 100% in winter too, as on the EPEVER display.

Output `soc_pct` is 0–100% of total, matching the EPEVER controller display exactly.

### 5. Day 3+ SOC reset

Days 1 and 2 carry forward the actual simulated SOC from the live API reading. From day 3 onwards, at midnight the battery resets to **75% total SOC** (= 50% usable = mid-range worst-case assumption). This is shown on operator charts as a dashed amber reference line.

### 6. Always-on background loads

- **Clyde CP2:** `always_on_load_w = 13.0W` (Currys Essentials fridge CTT50W12, confirmed from manual)
- **All other sites:** `always_on_load_w = 0.0W`
- **Inverter idle draw** runs 24/7 at all sites

The 13W fridge + inverter idle produces approximately 3.9% overnight SOC drop for CP2 (37W total × 10h ÷ 9600Wh), which matches the measured winter average of 3.6%.

### 7. Full-charge prediction

Linear regression model ported from the partner's JavaScript implementation. Predicts the time at which the battery will reach 100% SOC based on the day's forecast irradiance profile.

---

## JavaScript — `energyCalc.js`

All browser-side calculations use data from the JSON. No physics is re-derived in the browser.

### `calcAvailableEnergy()`

Returns three energy budget values:

| Value | Meaning |
|---|---|
| `batteryAboveFloor` | Energy currently stored above the DoD floor |
| `pvDuringLoadWh` | PV generated during the load runtime (serves load directly — no battery needed) |
| `immediateAvailableWh` | `batteryAboveFloor + pvDuringLoadWh` — what's available right now |
| `totalAvailableWh` | `batteryAboveFloor + remainingPvTotalWh` — what will be available by end of day |

`pv_available_w` (not `pv_w`) is used for `remainingPvTotalWh` to account for PV that the Python model curtailed in float mode.

### `calcFeasibility()`

Determines whether an appliance load can run:

| Status | Condition |
|---|---|
| `go` | `requestedWh ≤ immediateAvailableWh` — can start right now |
| `wait` | `requestedWh ≤ totalAvailableWh` — solar will charge enough; shows estimated ready time |
| `insufficient` | Even the full-day energy budget won't cover it |

### `deriveStatus()`

Determines the status banner shown to users:

| Status | Condition |
|---|---|
| `curtailment` | SOC ≥ 99% or voltage ≥ charge threshold — battery full, PV being curtailed |
| `charging` | PV > 0 |
| `low` | SOC < 55% (DoD floor + 5% buffer) |
| `critical` | SOC < 50% (at or below DoD floor) |
| `idle` | Overnight, no PV |

Note: voltage is `null` in all forecast rows (not available from Open-Meteo). `deriveStatus()` falls back to `SOC ≥ 99%` for curtailment detection when voltage is unavailable.

---

## Operator Charts — `ForecastChartsPanel.jsx`

### PV bar display

| Bar colour | Meaning |
|---|---|
| Bright | `pv_bright` — PV doing useful work (charging battery or serving load) |
| Faint | `pv_faint` — PV curtailed (battery full, no load) |

Total bar height equals `pv_available`. Bright = `pv_w` (used) plus any curtailed PV a scheduled load unlocks; faint = the rest of the curtailed PV.

### Load simulation — `simulateWithLoads()`

Calculates how a scheduled load affects the SOC line without re-simulating PV charging (which would double-count energy).

```
socWithLoad = pythonSoc - (cumulativeDrainWh / usableCapWh × 100)
```

Three-state logic mirrors the Python battery model:

| Condition | `hourDrainWh` |
|---|---|
| Float + small load (State 3) | `0` — load is free from curtailed PV |
| Float + large load (State 2) | `load - pvPeak` |
| Bulk charging or night | `loadW` |

The SOC-with-load line is displayed in blue (`#4a9eff`), dashed.

### Drag-and-drop scheduler

- Appliances are dragged from a palette onto hour bins (05:00–23:00)
- Loads are keyed by `date:hour` — a Friday 10:00 load only affects Friday's simulation
- Click a scheduled load pill to remove it
- A day selector controls which of the 7 days receives dropped loads

---

## Validation

### Clyde CP1 / CP2 — `validate_clyde.py`

Compares model predictions against `clydecycle1.xlsx` measured data (August 2025 – February 2026).

**Why ERA5, not Solcast:** The GTI column in the Clyde xlsx files comes from Solcast embedded in the controller export. ERA5 gives better correlation with Open-Meteo (R = 0.817 vs 0.737 for Solcast) and is consistent with the weather source the simulation uses.

**Valid window detection:**
- **Window A** — bulk charging morning: SOC rising, battery current > 0.5A, SOC < 90%
- **Window B** — load spike events: PV rising and SOC falling simultaneously

**Result:** ERA5 calibration factor = 0.876, used as `system_derating` for both Clyde sites.

### Sunny Cycles — `sweep_sunny_cycles.py`

Panel identification sweep across two installation phases:
- Phase 1 (2 panels, pre-October 2025): best fit 270W panels
- Phase 2 (8 panels, post-October 2025): ERA5 best fit approximately 200W×6 + 440W×2, ongoing

### Remaining sites

Chryston, Cumbernauld, Denmilne: no measured data available yet. `system_derating = 1.0` pending validation.
