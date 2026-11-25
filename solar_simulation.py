#!/usr/bin/env python3
"""
solar_simulation.py
===================
Off-grid solar PV simulation for VIP community cycling sites.

Architecture
------------
  BLOCK 1 : Site & panel configuration (datasheet values, no cal factors)
  BLOCK 2 : Solar Guardian API  — live battery SOC per site
  BLOCK 3 : Weather             — ERA5 (historical) or Open-Meteo (forecast)
  BLOCK 4 : Solar geometry + GTI transposition
  BLOCK 5 : PV model            — pure physics, no correction factors
  BLOCK 6 : Battery model       — hourly SOC simulation
  BLOCK 7 : Full-charge prediction (linear regression model)
  BLOCK 8 : GitHub output       — writes JSON to local repo path for Pages
  BLOCK 9 : Main orchestration

Each site has one correction factor, `system_derating` (Block 1), for real-world
losses the physics does not capture. 1.0 means "not yet validated".

Solar Guardian credentials are read from the SG_APP_KEY and SG_APP_SECRET
environment variables (GitHub secrets in the hourly workflow). They are never
written in this file. Without them the script still runs, using FALLBACK_SOC.
"""

import os
import re
import math
import json
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# ════════════════════════════════════════════════════════════════════
# BLOCK 1 — SITE & PANEL CONFIGURATION
# ════════════════════════════════════════════════════════════════════
#
# All values from datasheets — see solar_system_populated.xlsx.
# No calibration factors applied.
# ────────────────────────────────────────────────────────────────────

# ── Panel library ────────────────────────────────────────────────────
# (pmax_w, area_m2, gamma_pdc /°C, noct_°C)
PANELS = {
    "SSP130M":       (130,  1.179 * 0.664, -0.0040, 45.0),
    "UP-M250P":      (250,  1.640 * 0.992, -0.0043, 45.0),
    "STP250S-20/Wd": (250,  1.640 * 0.992, -0.0043, 45.0),
    "STP235S-20/Wd": (235,  1.640 * 0.992, -0.0043, 45.0),
    "OR10H450MNDB":  (450,  1.722 * 1.134, -0.0029, 43.0),
}


def mixed_array_specs(panels):
    """
    Derive weighted array specs from a mixed panel list.
    panels: list of (count, pmax_w, area_m2, gamma_pdc, noct)
    Returns (total_kw, avg_eff, area_per_kw, avg_gamma, avg_noct)
    """
    total_w    = sum(c * p for c, p, *_ in panels)
    total_area = sum(c * a for c, _, a, *_ in panels)
    avg_gamma  = sum(c * p * g for c, p, _, g, _ in panels) / total_w
    avg_noct   = sum(c * p * n for c, p, _, _, n in panels) / total_w
    avg_eff    = total_w / (total_area * 1000.0)
    area_per_kw = total_area / (total_w / 1000.0)
    return total_w / 1000.0, avg_eff, area_per_kw, avg_gamma, avg_noct


# ── Per-site array specs ─────────────────────────────────────────────
CHR_KW,  CHR_EFF,  CHR_APK,  CHR_GAMMA,  CHR_NOCT  = mixed_array_specs([(6,  *PANELS["SSP130M"])])
SC_KW,   SC_EFF,   SC_APK,   SC_GAMMA,   SC_NOCT   = mixed_array_specs([(2,  *PANELS["UP-M250P"]),
                                                                          (6,  *PANELS["STP250S-20/Wd"])])
CUM_KW,  CUM_EFF,  CUM_APK,  CUM_GAMMA,  CUM_NOCT  = mixed_array_specs([(4,  *PANELS["UP-M250P"]),
                                                                           (3,  *PANELS["SSP130M"])])
DEN_KW,  DEN_EFF,  DEN_APK,  DEN_GAMMA,  DEN_NOCT  = mixed_array_specs([(3,  *PANELS["STP235S-20/Wd"]),
                                                                           (6,  *PANELS["STP250S-20/Wd"])])
CLP_KW,  CLP_EFF,  CLP_APK,  CLP_GAMMA,  CLP_NOCT  = mixed_array_specs([(12, *PANELS["OR10H450MNDB"])])


LOCATIONS = [
    {
        "name":                  "Chryston",
        "solar_guardian_name":   "Chryston Powerhub",
        "system_derating":       1.0,   # not yet validated — use 1.0
        "lat": 55.9054, "lon": -4.1011,
        "capacity":              CHR_KW,
        "tilt": 45,  "azim": 123,
        "pv_eff":                CHR_EFF,
        "area_per_kw":           CHR_APK,
        "gamma_pdc":             CHR_GAMMA,
        "noct":                  CHR_NOCT,
        "bifacial_gain":         0.0,
        "system_loss":           0.05,
        "battery_usable_kwh":    1.92,
        "battery_rte":           0.85,
        "battery_cold_derate":   0.76,
        "mppt_max_power_w":      780.0,
        "inverter_cont_power_w": 1000.0,
        "inverter_idle_w":       9.6,
        "inverter_eff_full":     0.90,
        "inverter_eff_third":    0.95,
        "system_voltage":        24,
    },
    {
        "name":                  "Sunny Cycles",
        "solar_guardian_name":   "Sunny Cycles",
        "system_derating":       0.85,   # estimated — 24V system, shorter string
        "lat": 55.8570, "lon": -4.2378,
        "capacity":              SC_KW,
        "tilt": 0,   "azim": 180,
        "pv_eff":                SC_EFF,
        "area_per_kw":           SC_APK,
        "gamma_pdc":             SC_GAMMA,
        "noct":                  SC_NOCT,
        "bifacial_gain":         0.0,
        "system_loss":           0.05,
        "battery_usable_kwh":    1.32,
        "battery_rte":           0.85,
        "battery_cold_derate":   0.76,
        "mppt_max_power_w":      780.0,
        "inverter_cont_power_w": 1500.0,
        "inverter_idle_w":       9.6,
        "inverter_eff_full":     0.90,
        "inverter_eff_third":    0.95,
        "system_voltage":        24,
    },
    {
        "name":                  "Cumbernauld",
        "solar_guardian_name":   "Cumbernauld Village",
        "system_derating":       1.0,   # not yet validated
        "lat": 55.9629, "lon": -3.9766,
        "capacity":              CUM_KW,
        "tilt": 30,  "azim": 247,
        "pv_eff":                CUM_EFF,
        "area_per_kw":           CUM_APK,
        "gamma_pdc":             CUM_GAMMA,
        "noct":                  CUM_NOCT,
        "bifacial_gain":         0.0,
        "system_loss":           0.05,
        "battery_usable_kwh":    1.50,
        "battery_rte":           0.85,
        "battery_cold_derate":   0.83,
        "mppt_max_power_w":      520.0,
        "inverter_cont_power_w": 1500.0,
        "inverter_idle_w":       9.6,
        "inverter_eff_full":     0.90,
        "inverter_eff_third":    0.95,
        "system_voltage":        24,
    },
    {
        "name":                  "Denmilne",
        "solar_guardian_name":   "Denmilne Summerhouse",
        "system_derating":       1.0,   # not yet validated
        "lat": 56.3500, "lon": -2.7900,
        "capacity":              DEN_KW,
        "tilt": 0,   "azim": 180,
        "pv_eff":                DEN_EFF,
        "area_per_kw":           DEN_APK,
        "gamma_pdc":             DEN_GAMMA,
        "noct":                  DEN_NOCT,
        "bifacial_gain":         0.0,
        "system_loss":           0.05,
        "battery_usable_kwh":    2.40,
        "battery_rte":           0.85,
        "battery_cold_derate":   0.76,
        "mppt_max_power_w":      780.0,
        "inverter_cont_power_w": 2000.0,
        "inverter_idle_w":       9.6,
        "inverter_eff_full":     0.90,
        "inverter_eff_third":    0.95,
        "system_voltage":        24,
    },
    {
        "name":                  "Clyde CP1",
        "solar_guardian_name":   "Clyde Cycle Park",
        "system_derating":       0.876,  # validated: ERA5 cal=0.876 from validate_clyde.py
        "lat": 55.8310, "lon": -4.1836,
        "capacity":              CLP_KW,
        "tilt": 45,  "azim": 180,
        "pv_eff":                CLP_EFF,
        "area_per_kw":           CLP_APK,
        "gamma_pdc":             CLP_GAMMA,
        "noct":                  CLP_NOCT,
        "bifacial_gain":         0.0,   # set to 0 until validated
        "system_loss":           0.05,
        "battery_usable_kwh":    4.80,
        "battery_rte":           0.85,
        "battery_cold_derate":   0.76,
        "mppt_max_power_w":      4800.0,
        "inverter_cont_power_w": 5000.0,
        "inverter_idle_w":       24.0,
        # CP1 and CP2 share one Solar Guardian station. Set this to CP1's device
        # name exactly as the log prints it ("Clyde Cycle Park / <device>") so
        # each controller gets its own battery level.
        "solar_guardian_device": None,
        "always_on_load_w":      0.0,           # no confirmed always-on load at CP1
        "inverter_eff_full":     0.91,
        "inverter_eff_third":    0.94,
        "system_voltage":        48,
    },
    {
        "name":                  "Clyde CP2",
        "solar_guardian_name":   "Clyde Cycle Park",
        "system_derating":       0.876,  # validated: ERA5 cal=0.876 from validate_clyde.py
        "lat": 55.8310, "lon": -4.1836,
        "capacity":              CLP_KW,
        "tilt": 45,  "azim": 180,
        "pv_eff":                CLP_EFF,
        "area_per_kw":           CLP_APK,
        "gamma_pdc":             CLP_GAMMA,
        "noct":                  CLP_NOCT,
        "bifacial_gain":         0.0,
        "system_loss":           0.05,
        "battery_usable_kwh":    4.80,
        "battery_rte":           0.85,
        "battery_cold_derate":   0.76,
        "mppt_max_power_w":      4800.0,
        "inverter_cont_power_w": 5000.0,
        "inverter_idle_w":       24.0,
        "solar_guardian_device": None,          # set to CP2's device name — see CP1
        "always_on_load_w":      13.0,          # fridge CTT50W12 confirmed 12.3W avg
        "inverter_eff_full":     0.91,
        "inverter_eff_third":    0.94,
        "system_voltage":        48,
    },
]

# ── Simulation config ────────────────────────────────────────────────
FORECAST_DAYS   = 7        # how many days ahead to simulate
ALBEDO          = 0.20
G_SC            = 1367     # solar constant W/m²
# Used when no live SOC is available. Fraction of TOTAL capacity, like the
# EPEVER display: 0.75 total = half of the usable range (the floor is 0.50).
FALLBACK_SOC    = 0.75


# ════════════════════════════════════════════════════════════════════
# BLOCK 2 — SOLAR GUARDIAN API (live battery SOC)
# ════════════════════════════════════════════════════════════════════
#
# Fetches the most recent BAT_SOC reading for each site from the
# EPSolar cloud API. Falls back to FALLBACK_SOC if unavailable.
# ────────────────────────────────────────────────────────────────────

# Never put real values here — set SG_APP_KEY / SG_APP_SECRET as environment
# variables (locally) or GitHub repository secrets (hourly workflow).
SOLAR_GUARDIAN_APP_KEY    = os.getenv("SG_APP_KEY", "")
SOLAR_GUARDIAN_APP_SECRET = os.getenv("SG_APP_SECRET", "")
SOLAR_GUARDIAN_BASE_URL   = "https://openapi.epsolarpv.com"
SOLAR_GUARDIAN_HISTORY_URL = f"{SOLAR_GUARDIAN_BASE_URL}:7002/history/lastDatapoint"


def _sg_post(url: str, token: Optional[str], payload: Dict) -> Dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Access-Token"] = token
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def _sg_get_token() -> Optional[str]:
    try:
        body = _sg_post(
            f"{SOLAR_GUARDIAN_BASE_URL}/epCloud/user/getAuthToken", None,
            {"appKey": SOLAR_GUARDIAN_APP_KEY, "appSecret": SOLAR_GUARDIAN_APP_SECRET},
        )
        return body.get("data", {}).get("X-Access-Token")
    except Exception as e:
        print(f"  [Solar Guardian] Auth failed: {e}")
        return None


def _sg_get_stations(token: str) -> List[Dict]:
    try:
        body = _sg_post(
            f"{SOLAR_GUARDIAN_BASE_URL}/epCloud/vn/openApi/getPowerStationListPage",
            token, {"pageNo": 1, "pageSize": 100},
        )
        return [
            {"id": s.get("id"), "name": s.get("powerStationName")}
            for s in body.get("data", {}).get("list", [])
            if s.get("id") and s.get("powerStationName")
        ]
    except Exception as e:
        print(f"  [Solar Guardian] Station list failed: {e}")
        return []


def _sg_get_devices(token: str, station_id: str) -> List[Dict]:
    try:
        body = _sg_post(
            f"{SOLAR_GUARDIAN_BASE_URL}/epCloud/vn/openApi/getEquipmentList",
            token, {"powerStationId": station_id, "pageNo": 1, "pageSize": 100},
        )
        if body.get("status") != 0:
            return []
        return [
            {"id": d.get("id"), "name": d.get("equipmentName")}
            for d in (body.get("data") or {}).get("list", [])
            if d.get("id")
        ]
    except Exception as e:
        print(f"  [Solar Guardian] Device list failed: {e}")
        return []


def _sg_get_soc_point(token: str, device_id: str) -> Optional[Dict]:
    """Find the BAT_SOC datapoint descriptor for a device."""
    try:
        body = _sg_post(
            f"{SOLAR_GUARDIAN_BASE_URL}/epCloud/vn/openApi/getEquipment",
            token, {"id": device_id},
        )
        if body.get("status") != 0:
            return None
        for group in (body.get("data") or {}).get("variableGroupList", []):
            for v in group.get("variableList", []):
                if v.get("dataIdentifier") == "BAT_SOC" and v.get("dataPointId"):
                    return {
                        "dataPointId": v["dataPointId"],
                        "deviceNo":    v.get("deviceNo", ""),
                        "slaveIndex":  v.get("slaveIndex", ""),
                        "itemId":      v.get("itemId", ""),
                    }
    except Exception as e:
        print(f"  [Solar Guardian] getEquipment failed: {e}")
    return None


def _sg_fetch_last_soc(token: str, point: Dict) -> Optional[float]:
    """Fetch the most recent BAT_SOC value for one datapoint."""
    try:
        body = _sg_post(
            SOLAR_GUARDIAN_HISTORY_URL, token,
            {"devDatapoints": [{
                "deviceNo":   point["deviceNo"],
                "slaveIndex": point["slaveIndex"],
                "itemId":     point["itemId"],
                "dataPointId": point["dataPointId"],
            }]},
        )
        items = body.get("data") or []
        if isinstance(items, dict):
            items = items.get("list", [])
        for it in items:
            val = it.get("value")
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"  [Solar Guardian] lastDatapoint failed: {e}")
    return None


def soc_key(station: str, device: Optional[str] = None) -> str:
    """Key used in the live-SOC map: 'Station' or 'Station / Device'."""
    return f"{station} / {device}" if device else station


def fetch_live_soc() -> Dict[str, float]:
    """
    Returns a dict of SOC readings (0–100), keyed two ways:
      'Station / Device' — one entry per charge controller
      'Station'          — the station's first controller (for single-controller sites)
    Sites missing from the dict fall back to FALLBACK_SOC.
    """
    print("\n[Block 2] Fetching live SOC from Solar Guardian API ...")
    soc_map: Dict[str, float] = {}

    if not (SOLAR_GUARDIAN_APP_KEY and SOLAR_GUARDIAN_APP_SECRET):
        print("  SG_APP_KEY / SG_APP_SECRET not set — using fallback SOC for all sites.")
        return soc_map

    token = _sg_get_token()
    if not token:
        print("  Could not authenticate — using fallback SOC for all sites.")
        return soc_map

    stations = _sg_get_stations(token)
    print(f"  Found {len(stations)} station(s): {[s['name'] for s in stations]}")

    for station in stations:
        devices = _sg_get_devices(token, station["id"])
        for device in devices:
            point = _sg_get_soc_point(token, device["id"])
            if not point:
                continue
            soc = _sg_fetch_last_soc(token, point)
            if soc is not None:
                # Every controller gets its own entry, so stations with two
                # controllers (Clyde CP1 + CP2) are not merged into one.
                soc_map[soc_key(station["name"], device["name"])] = float(soc)
                soc_map.setdefault(station["name"], float(soc))
                print(f"  {soc_key(station['name'], device['name'])}: SOC = {soc:.1f}%")

    return soc_map


# ════════════════════════════════════════════════════════════════════
# BLOCK 3 — WEATHER  (ERA5 historical  +  Open-Meteo forecast)
# ════════════════════════════════════════════════════════════════════

def _fetch_era5(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    r = requests.get(
        "https://archive-api.open-meteo.com/v1/era5",
        params={
            "latitude": lat, "longitude": lon,
            "start_date": start, "end_date": end,
            "hourly": "temperature_2m,shortwave_radiation,"
                      "direct_radiation,diffuse_radiation",
            "timezone": "UTC",
        },
        timeout=30,
    )
    r.raise_for_status()
    df = pd.DataFrame(r.json()["hourly"])
    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize("UTC")
    return df.set_index("time")


def _fetch_forecast(lat: float, lon: float, forecast_days: int,
                    past_days: int) -> pd.DataFrame:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,shortwave_radiation,"
                      "direct_radiation,diffuse_radiation",
            "timezone": "UTC",
            "past_days": past_days,
            "forecast_days": forecast_days,
        },
        timeout=30,
    )
    r.raise_for_status()
    df = pd.DataFrame(r.json()["hourly"])
    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize("UTC")
    return df.set_index("time")


def get_weather(lat: float, lon: float,
                start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> pd.DataFrame:
    """
    Fetch weather for [start_dt, end_dt).
    Uses ERA5 for past dates, Open-Meteo forecast for future,
    and stitches both for ranges that span today.
    """
    now = pd.Timestamp.now(tz="UTC").floor("h")

    if end_dt <= now:
        # fully historical
        df = _fetch_era5(lat, lon,
                         start_dt.date().isoformat(),
                         end_dt.date().isoformat())
        mode = "ERA5"
    elif start_dt >= now:
        # fully future
        days_ahead = math.ceil((end_dt - now).total_seconds() / 86400)
        df = _fetch_forecast(lat, lon, forecast_days=min(days_ahead, 16), past_days=0)
        mode = "forecast"
    else:
        # straddles today — Open-Meteo forecast with past_days=1 covers
        # from midnight yesterday through 16 days ahead in a single call,
        # so we get today's already-elapsed hours AND the future forecast
        fc   = _fetch_forecast(lat, lon, forecast_days=FORECAST_DAYS, past_days=1)
        df   = fc.sort_index()
        df   = df[~df.index.duplicated(keep="last")]
        mode = "forecast"

    df = df.loc[(df.index >= start_dt) & (df.index < end_dt)]
    df = df.rename(columns={
        "shortwave_radiation": "Ggh",
        "direct_radiation":    "Gbh",
        "diffuse_radiation":   "Gdh",
        "temperature_2m":      "Tamb",
    })
    for col in ["Ggh", "Gbh", "Gdh", "Tamb"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df, mode


# ════════════════════════════════════════════════════════════════════
# BLOCK 4 — SOLAR GEOMETRY + GTI TRANSPOSITION
# ════════════════════════════════════════════════════════════════════

DEG2RAD = np.pi / 180.0


def _day_number(ts_utc: pd.DatetimeIndex) -> np.ndarray:
    return ts_utc.tz_convert("Europe/London").dayofyear.values.astype(float)


def _declination(n: np.ndarray) -> np.ndarray:
    return 23.45 * np.sin(DEG2RAD * (360 * (284 + n) / 365))


def _eccentricity(n: np.ndarray) -> np.ndarray:
    return 1 + 0.033 * np.cos(DEG2RAD * (360 * n / 365))


def _hour_angle(ts_utc: pd.DatetimeIndex, lon: float) -> np.ndarray:
    local = ts_utc.tz_convert("Europe/London")
    n     = local.dayofyear.values.astype(float)
    B     = 2 * np.pi * (n - 81) / 364
    eot   = 9.87 * np.sin(2*B) - 7.53 * np.cos(B) - 1.5 * np.sin(B)
    tz_off = np.array([
        t.utcoffset().total_seconds() / 3600.0
        for t in local
    ])
    solar_mins = (local.hour * 60 + local.minute).astype(float) + eot + 4*lon - 60*tz_off
    return 0.25 * solar_mins - 180.0


def _cos_zenith(lat: float, dec: np.ndarray, omega: np.ndarray) -> np.ndarray:
    φ = lat * DEG2RAD
    return (np.sin(φ) * np.sin(dec * DEG2RAD)
            + np.cos(φ) * np.cos(dec * DEG2RAD) * np.cos(omega * DEG2RAD))


def _cos_incidence(lat: float, tilt: float, azim: float,
                   dec: np.ndarray, omega: np.ndarray) -> np.ndarray:
    φ = lat  * DEG2RAD
    β = tilt * DEG2RAD
    γ = (azim - 180.0) * DEG2RAD   # from-south convention
    δ = dec   * DEG2RAD
    ω = omega * DEG2RAD
    return (
        (np.sin(φ)*np.cos(β) - np.cos(φ)*np.sin(β)*np.cos(γ)) * np.sin(δ)
      + (np.cos(φ)*np.cos(β) + np.sin(φ)*np.sin(β)*np.cos(γ)) * np.cos(δ)*np.cos(ω)
      +  np.cos(δ) * np.sin(β) * np.sin(γ) * np.sin(ω)
    )


def compute_gti(weather_df: pd.DataFrame, lat: float, lon: float,
                tilt: float, azim: float, bifacial_gain: float) -> pd.DataFrame:
    """
    Compute GTI on the panel plane using isotropic sky transposition.
    Bifacial rear gain applied as a flat multiplier (0 = monofacial).
    Returns weather_df with added columns: gti, cos_zen, Gbn, Tamb.
    """
    idx   = weather_df.index
    n     = _day_number(idx)
    dec   = _declination(n)
    omega = _hour_angle(idx, lon)

    cos_zen = np.maximum(_cos_zenith(lat, dec, omega), 0.0)
    cos_inc = np.maximum(_cos_incidence(lat, tilt, azim, dec, omega), 0.0)

    Ggh = weather_df["Ggh"].values
    Gdh = weather_df["Gdh"].values
    Gbh = weather_df["Gbh"].values

    # Beam normal from direct horizontal; cap at 1400 W/m²
    Gbn = np.minimum(Gbh / np.maximum(cos_zen, 1e-6), 1400.0)
    Gbn = np.maximum(Gbn, 0.0)

    # Isotropic transposition
    β_r = tilt * DEG2RAD
    Gbt = Gbn * cos_inc
    Gdt = Gdh * (1 + np.cos(β_r)) / 2.0
    Grt = (Gbh + Gdh) * ALBEDO * (1 - np.cos(β_r)) / 2.0
    gti = np.maximum(Gbt + Gdt + Grt, 0.0)

    # Bifacial rear gain
    gti = gti * (1.0 + bifacial_gain)

    out = weather_df.copy()
    out["gti"]     = gti
    out["Gbn"]     = Gbn
    out["cos_zen"] = cos_zen
    return out


# ════════════════════════════════════════════════════════════════════
# BLOCK 5 — PV MODEL  (pure physics, no correction factors)
# ════════════════════════════════════════════════════════════════════

def simulate_pv(loc: Dict, irr_df: pd.DataFrame) -> pd.Series:
    """
    Hourly AC PV output (W).

    Steps:
      1. Cell temperature from NOCT
      2. Temperature-derated efficiency (γ from datasheet)
      3. DC power = GTI × area × eff_T × (1 − system_loss)
      4. MPPT clip at controller rated power
      5. Inverter efficiency (two-point model: full vs third load)
      6. Inverter output cap at rated AC power
    """
    gti  = irr_df["gti"].values
    tamb = irr_df["Tamb"].values

    # Cell temperature
    Tc = tamb + (loc["noct"] - 20.0) / 800.0 * gti

    # Temperature-derated efficiency
    eff_T = loc["pv_eff"] * (1.0 + loc["gamma_pdc"] * (Tc - 25.0))

    # DC power
    area = loc["capacity"] * loc["area_per_kw"]
    Pdc  = np.maximum(gti * area * eff_T * (1.0 - loc["system_loss"]), 0.0)

    # Site-specific derating (wiring topology, string mismatch, soiling).
    # Derived from validation against measured data — see LOCATIONS config.
    # Applied before MPPT clip so clipping behaviour is preserved correctly.
    Pdc = Pdc * loc.get("system_derating", 1.0)

    # MPPT clip
    Pdc = np.minimum(Pdc, loc["mppt_max_power_w"])

    # Inverter
    inv_rated = loc["inverter_cont_power_w"]
    inv_eff   = np.where(Pdc >= 0.5 * inv_rated,
                         loc["inverter_eff_full"], loc["inverter_eff_third"])
    Pac = np.minimum(Pdc * inv_eff, inv_rated)

    return pd.Series(Pac, index=irr_df.index, name="pv_w")


# ════════════════════════════════════════════════════════════════════
# BLOCK 6 — BATTERY MODEL
# ════════════════════════════════════════════════════════════════════

def _cold_derate_factor(bat_temp: float, loc: Dict) -> float:
    """Linear derate between 0°C (full cold derate) and 10°C (no derate)."""
    if bat_temp >= 10.0:
        return 1.0
    if bat_temp <= 0.0:
        return loc["battery_cold_derate"]
    frac = bat_temp / 10.0
    return frac + (1.0 - frac) * loc["battery_cold_derate"]


def simulate_battery(loc: Dict, pv_series: pd.Series,
                     irr_df: pd.DataFrame,
                     initial_soc_frac: float) -> Dict:
    """
    Hourly battery simulation.

    Inputs
    ------
    initial_soc_frac : SOC at the start of the first hour (0–1).
                       Sourced from Solar Guardian API if available,
                       otherwise falls back to FALLBACK_SOC.

    Returns
    -------
    Dict with:
      soc_pct   : pd.Series of hourly SOC (% of total, EPEVER convention)
      pv_w      : pd.Series of PV actually used (W): load + battery charging.
                  Lower than pv_series when the battery is full (curtailed).
      daily     : Dict of per-day summaries (gen_kwh = used, available_kwh)
    """
    # SOC is tracked as a FRACTION of the battery's effective capacity, like the
    # EPEVER display: 1.0 = full, 0.5 = the DoD floor (AGM, 50% depth of discharge).
    # Cold weather shrinks the effective capacity (battery_cold_derate), but a full
    # battery still reads 100% — the controller's display works the same way.
    usable_cap   = loc["battery_usable_kwh"]
    total_cap    = usable_cap * 2.0              # total = 2× usable (50% DoD)
    floor_frac   = 0.50                          # never discharge below 50% of total
    rte          = loc["battery_rte"]
    idle_kw      = loc["inverter_idle_w"] / 1000.0
    local_idx    = pv_series.index.tz_convert("Europe/London")

    soc_frac = min(1.0, max(floor_frac, initial_soc_frac))   # seed from EPEVER %
    soc_arr  = np.zeros(len(pv_series))
    pv_used  = np.zeros(len(pv_series))
    daily    = {}

    # From day 3 on, reset SOC each midnight to a cautious mid-range value
    today_utc    = pd.Timestamp.now(tz="UTC").normalize()
    day3_cutoff  = today_utc + pd.Timedelta(days=2)   # start of day 3
    current_date = None

    for i, (ts_local, ts_utc) in enumerate(zip(local_idx, pv_series.index)):
        date_str = str(ts_local.date())
        hour_dec = ts_local.hour + ts_local.minute / 60.0

        if date_str != current_date:
            current_date = date_str
            if ts_utc >= day3_cutoff and hour_dec < 1.0:
                soc_frac = 0.75   # 75% total = 50% of the usable range

        # Effective capacity this hour (cold derate)
        bat_temp = float(irr_df.loc[ts_utc, "Tamb"])
        eff_cap  = total_cap * _cold_derate_factor(bat_temp, loc)
        floor_kwh = eff_cap * floor_frac
        soc_kwh   = soc_frac * eff_cap

        # PV the array can deliver this hour (already MPPT- and inverter-limited)
        pv_avail_kw = pv_series.iloc[i] / 1000.0

        # Background load runs 24/7: inverter idle + confirmed always-on appliances.
        # Validated: winter overnight drop implies ~37W for CP2 (24W+13W = 37W)
        load_kw = idle_kw + loc.get("always_on_load_w", 0.0) / 1000.0

        # ── Charge controller ────────────────────────────────────────────────
        # PV serves the load first. Any surplus charges the battery, but only
        # as much as the battery can still take: once it is full the controller
        # curtails the rest (that energy is never generated). This one rule
        # covers all three EPEVER states seen in the measured data:
        #   bulk       — battery not full: surplus charges it
        #   float      — full, load > PV: battery covers the shortfall
        #   float/free — full, load <= PV: PV covers the load, rest curtailed
        pv_to_load   = min(pv_avail_kw, load_kw)
        shortfall    = load_kw - pv_to_load
        surplus      = pv_avail_kw - pv_to_load
        headroom_kwh = max(0.0, eff_cap - soc_kwh)
        charge_kwh   = min(surplus * rte, headroom_kwh)     # energy stored (1 h step)
        pv_to_batt   = charge_kwh / rte if rte > 0 else 0.0
        gen_kw       = pv_to_load + pv_to_batt              # PV actually used

        soc_kwh  = max(floor_kwh, min(eff_cap, soc_kwh + charge_kwh - shortfall))
        soc_frac = soc_kwh / eff_cap if eff_cap > 0 else floor_frac

        soc_arr[i] = soc_frac * 100.0
        pv_used[i] = gen_kw * 1000.0

        # Daily stats
        if date_str not in daily:
            daily[date_str] = {"gen_kwh": 0.0, "available_kwh": 0.0,
                               "full_charge_hour": None}
        daily[date_str]["gen_kwh"]       += gen_kw
        daily[date_str]["available_kwh"] += pv_avail_kw

        if (daily[date_str]["full_charge_hour"] is None
                and soc_frac >= 0.99
                and pv_avail_kw > 0.0     # only record when solar is actively charging
                and hour_dec >= 5.0):     # ignore midnight/pre-dawn artefacts
            daily[date_str]["full_charge_hour"] = round(hour_dec, 2)

    return {
        "soc_pct": pd.Series(soc_arr, index=pv_series.index, name="soc_pct"),
        "pv_w":    pd.Series(pv_used, index=pv_series.index, name="pv_w"),
        "daily":   daily,
    }


# ════════════════════════════════════════════════════════════════════
# BLOCK 7 — FULL-CHARGE PREDICTION (linear regression)
# ════════════════════════════════════════════════════════════════════
#
# Ported from partner's JS model, trained on real Clyde CP1 data.
# Applied to all sites using their respective GTI inputs.
# ────────────────────────────────────────────────────────────────────

_FC_MODEL = {
    "intercept": 10.072815533980583,
    "features":  ["soc_8am", "forecast_morn_gti", "forecast_morn_cs_ratio",
                  "gti_std", "soc_deficit", "forecast_mean_gti", "bat_temp_morn"],
    "means":     [65.51779935275081, 141.11972115707127, 0.46383934177507047,
                  133.9933002845878, 44.75728155339806, 94.75221465247303, 13.340996133506875],
    "stds":      [20.470441138906732, 126.68579416347106, 0.2539083299272435,
                  71.45573994275193, 6.798366479148161, 63.30256564504041, 5.5386620317703406],
    "coefs":     [-1.440679054105104, 0.7750471970521945, -0.7807648225487965,
                  -0.36030721126950715, -0.05499512558681864, 0.0745135986611796,
                  0.2391379737276998],
}


def predict_full_charge_hour(soc_8am: float, morn_gti: float,
                              morn_cs_ratio: float, gti_std: float,
                              mean_gti: float, bat_temp: float) -> Dict:
    """
    Predict decimal hour when battery reaches 100% SOC.
    Uses the linear regression model from partner's JS implementation.
    """
    soc_deficit = 100.0 - soc_8am
    values = [soc_8am, morn_gti, morn_cs_ratio, gti_std, soc_deficit, mean_gti, bat_temp]

    pred = _FC_MODEL["intercept"]
    for i, val in enumerate(values):
        v = val if np.isfinite(val) else _FC_MODEL["means"][i]
        pred += _FC_MODEL["coefs"][i] * (v - _FC_MODEL["means"][i]) / _FC_MODEL["stds"][i]

    pred = max(6.0, min(20.0, pred))
    h, m = int(pred), round((pred % 1) * 60)

    confidence = ("high"           if morn_cs_ratio > 0 and np.isfinite(morn_cs_ratio)
                  else "moderate"  if np.isfinite(soc_8am)
                  else "estimate")

    return {"hour": pred, "time_str": f"{h:02d}:{m:02d}", "confidence": confidence}


def compute_daily_fc_inputs(irr_df: pd.DataFrame, soc_map_8am: Dict) -> Dict:
    """
    Compute per-day inputs for the full-charge prediction model.
    soc_map_8am: {date_str: soc_%} — from battery sim or API.
    """
    local_idx = irr_df.index.tz_convert("Europe/London")
    daily = {}

    GON = G_SC  # simplified (no eccentricity here — minor effect)

    for date_str, grp in irr_df.groupby(local_idx.date):
        local = grp.index.tz_convert("Europe/London")
        h     = local.hour

        morn_mask = (h >= 6) & (h < 12)
        day_mask  = (h >= 6) & (h <= 18)

        gti_morn = grp["gti"].values[morn_mask]
        gti_day  = grp["gti"].values[day_mask]
        gti_all  = grp["gti"].values

        # Simple clear-sky estimate for cs_ratio
        cos_zen_morn = grp["cos_zen"].values[morn_mask]
        gti_cs_morn  = np.maximum(GON * cos_zen_morn * 0.7, 0.0)

        morn_mean  = float(np.mean(gti_morn)) if len(gti_morn) > 0 else 0.0
        cs_mean    = float(np.mean(gti_cs_morn)) if len(gti_cs_morn) > 0 else 0.0
        cs_ratio   = morn_mean / cs_mean if cs_mean > 1.0 else 0.0
        gti_std    = float(np.std(gti_all, ddof=1)) if len(gti_all) > 1 else 0.0
        mean_gti   = float(np.mean(gti_day)) if len(gti_day) > 0 else 0.0

        h8 = (h == 8)
        bat_temp = float(grp["Tamb"].values[h8].mean()) if h8.any() else 10.0

        soc_8am = soc_map_8am.get(str(date_str), FALLBACK_SOC * 100.0)

        daily[str(date_str)] = {
            "soc_8am":     soc_8am,
            "morn_gti":    morn_mean,
            "cs_ratio":    cs_ratio,
            "gti_std":     gti_std,
            "mean_gti":    mean_gti,
            "bat_temp":    bat_temp,
        }
    return daily


# ════════════════════════════════════════════════════════════════════
# BLOCK 8 — GITHUB / WEB OUTPUT
# ════════════════════════════════════════════════════════════════════
#
# Writes a JSON file to the local GitHub Pages repo path.
# The JSON is designed to be consumed directly by the website JS.
# Push/deploy is handled separately (GitHub Actions or manual git push).
# ────────────────────────────────────────────────────────────────────

# ── Configure this to your local repo path ──────────────────────────
GITHUB_REPO_PATH = os.getenv("GITHUB_REPO_PATH", os.path.expanduser(
    "~/Documents/VIP 3rd Year/VIP_Model"
))
OUTPUT_JSON_NAME  = "docs/data/simulation_output.json"   # served by GitHub Pages
OUTPUT_JSON_VITE  = "public/data/simulation_output.json" # served by Vite dev server
# ────────────────────────────────────────────────────────────────────


def _decimal_hour_to_str(h: Optional[float]) -> Optional[str]:
    if h is None:
        return None
    total_minutes = round(h * 60)
    hours   = (total_minutes // 60) % 24
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def build_output_payload(results: List[Dict], generated_at: str) -> Dict:
    """
    Build the JSON payload consumed by the GitHub Pages website.

    Structure
    ---------
    {
      "generated_at": "ISO timestamp",
      "sites": [
        {
          "name": "Chryston",
          "capacity_kwp": 0.78,
          "current_soc_pct": 52.0,
          "soc_source": "api" | "fallback",
          "weather_mode": "ERA5" | "forecast" | "mixed",
          "hourly": [
            {"time": "2025-03-19T08:00:00Z", "pv_w": 312.0, "soc_pct": 67.2},
            ...
          ],
          "daily": [
            {
              "date": "2025-03-19",
              "gen_kwh": 1.23,
              "full_charge_time": "13:45",
              "fc_confidence": "high"
            },
            ...
          ]
        },
        ...
      ]
    }
    """
    sites_out = []

    for r in results:
        loc         = r["loc"]
        soc_series  = r["battery"]["soc_pct"]
        pv_series   = r["battery"]["pv_w"]
        daily_bat   = r["battery"]["daily"]
        daily_fc    = r["daily_fc"]

        # Hourly records — downsample to every hour on the hour
        # pv_series = curtailed output (0 when battery full)
        # r["pv_uncurtailed"] = raw simulate_pv output before battery clipping
        pv_raw = r.get("pv_uncurtailed", pv_series)
        hourly = []
        for ts, pv_val, soc_val, pv_raw_val in zip(
            pv_series.index, pv_series.values, soc_series.values, pv_raw.values
        ):
            if ts.minute == 0:
                hourly.append({
                    "time":        ts.isoformat(),
                    "pv_w":        round(float(pv_val), 1),
                    "pv_available": round(float(pv_raw_val), 1),  # uncurtailed estimate
                    "soc_pct":     round(float(soc_val), 1),
                })

        # Daily records
        daily_out = []
        for date_str, bat_day in sorted(daily_bat.items()):
            fc_inputs = daily_fc.get(date_str, {})
            fc_pred   = None
            if fc_inputs:
                fc_pred = predict_full_charge_hour(
                    soc_8am       = fc_inputs["soc_8am"],
                    morn_gti      = fc_inputs["morn_gti"],
                    morn_cs_ratio = fc_inputs["cs_ratio"],
                    gti_std       = fc_inputs["gti_std"],
                    mean_gti      = fc_inputs["mean_gti"],
                    bat_temp      = fc_inputs["bat_temp"],
                )
            # Weather icon from AVAILABLE solar vs theoretical max — not from
            # gen_kwh, which drops on sunny days once the battery is full.
            # Theoretical max = capacity_kw * 8 daylight hours * 0.85 efficiency
            cap_kw       = loc["capacity"]
            theo_max_kwh = cap_kw * 8 * 0.85
            avail_kwh    = bat_day.get("available_kwh", bat_day["gen_kwh"])
            gen_ratio    = avail_kwh / theo_max_kwh if theo_max_kwh > 0 else 0
            if gen_ratio > 0.70:   weather_icon = "sunny"
            elif gen_ratio > 0.45: weather_icon = "partly_cloudy"
            elif gen_ratio > 0.20: weather_icon = "cloudy"
            else:                  weather_icon = "overcast"

            daily_out.append({
                "date":              date_str,
                "gen_kwh":           round(bat_day["gen_kwh"], 3),        # PV used
                "available_kwh":     round(bat_day.get("available_kwh", 0.0), 3),  # PV possible
                "full_charge_time":  _decimal_hour_to_str(bat_day.get("full_charge_hour")),
                "fc_model_time":     fc_pred["time_str"]   if fc_pred else None,
                "fc_confidence":     fc_pred["confidence"] if fc_pred else None,
                "weather_icon":      weather_icon,
            })

        sites_out.append({
            "name":            loc["name"],
            "capacity_kwp":    round(loc["capacity"], 3),
            "system_derating": loc.get("system_derating", 1.0),
            "current_soc_pct": round(float(soc_series.iloc[0]), 1),
            "soc_source":      r["soc_source"],
            "weather_mode":    r["weather_mode"],
            "hourly":          hourly,
            "daily":           daily_out,
        })

    return {"generated_at": generated_at, "sites": sites_out}


def write_output_json(payload: Dict) -> str:
    """
    Write payload to both:
      docs/data/  — for GitHub Pages (production)
      public/data/ — for Vite dev server (local development)
    Returns the docs path.
    """
    serialised = json.dumps(payload, indent=2, default=str)

    for rel_path in [OUTPUT_JSON_NAME, OUTPUT_JSON_VITE]:
        out_path = os.path.join(GITHUB_REPO_PATH, rel_path)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(serialised)
        print(f"  Written → {out_path}")

    return os.path.join(GITHUB_REPO_PATH, OUTPUT_JSON_NAME)


def _ensure_data_branch(repo_path: str, branch: str) -> None:
    """
    Switch to DATA_BRANCH (creating it from main if it doesn't exist yet).
    This branch only ever receives the JSON data file — no Python code.
    """
    import subprocess
    # Check if branch already exists remotely
    result = subprocess.run(
        ["git", "-C", repo_path, "ls-remote", "--heads", "origin", branch],
        capture_output=True, text=True,
    )
    if branch not in result.stdout:
        # Create the branch from main without switching working tree
        subprocess.run(
            ["git", "-C", repo_path, "branch", branch, "main"],
            capture_output=True,
        )
        print(f"  Created branch '{branch}' from main.")


def git_push(repo_path: str, commit_msg: str, branch: str = "main") -> None:
    """
    Stage ONLY the JSON data file, commit, and push to the specified branch.
    No Python source files are ever touched or pushed by this function.
    Requires git CLI on PATH.
    """
    import subprocess
    cmds = [
        ["git", "-C", repo_path, "add", OUTPUT_JSON_NAME],
        ["git", "-C", repo_path, "commit", "-m", commit_msg],
        ["git", "-C", repo_path, "push", "origin", f"HEAD:{branch}"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout + result.stderr:
                print(f"  git: nothing new to commit.")
                return
            print(f"  git error: {result.stderr.strip()}")
            return
    print(f"  Pushed → branch '{branch}': {commit_msg}")


# ════════════════════════════════════════════════════════════════════
# BLOCK 9 — MAIN ORCHESTRATION
# ════════════════════════════════════════════════════════════════════

def run():
    now_utc      = pd.Timestamp.now(tz="UTC").floor("h")
    start_dt     = now_utc.normalize()          # midnight today — gets full day from forecast API
    end_dt       = start_dt + pd.Timedelta(days=FORECAST_DAYS)
    generated_at = now_utc.isoformat()

    print(f"\n{'='*65}")
    print(f"  VIP Solar Simulation  |  {generated_at}")
    print(f"  Simulating: {start_dt.date()} → {end_dt.date()}  ({FORECAST_DAYS} days)")
    print(f"{'='*65}")

    # ── Block 2: Fetch live SOC ──────────────────────────────────
    live_soc = fetch_live_soc()

    results = []

    for loc in LOCATIONS:
        print(f"\n── {loc['name']}  ({loc['capacity']:.2f} kWp) ──────────────────────────")

        # Resolve initial SOC — per controller where a device name is set
        sg_name   = loc.get("solar_guardian_name", loc["name"])
        sg_device = loc.get("solar_guardian_device")
        device_key = soc_key(sg_name, sg_device)
        shared = sum(1 for l in LOCATIONS
                     if l.get("solar_guardian_name", l["name"]) == sg_name) > 1

        if sg_device and device_key in live_soc:
            soc_key_used, soc_source = device_key, "api"
        elif sg_name in live_soc:
            soc_key_used = sg_name
            # A station shared by two controllers without a device name set
            # gives both sites the same reading — flag it in the output.
            soc_source = "api_shared" if shared else "api"
            if sg_device:
                print(f"  WARNING: device '{sg_device}' not found; using station reading")
            elif shared:
                print(f"  WARNING: '{sg_name}' has several controllers — set "
                      f"solar_guardian_device for {loc['name']} (see device names above)")
        else:
            soc_key_used = None

        if soc_key_used:
            # EPEVER SOC is 0–100% of TOTAL capacity (DoD floor at 50%).
            # simulate_battery() works in the same convention.
            initial_soc_frac = live_soc[soc_key_used] / 100.0
            print(f"  SOC from API  : {live_soc[soc_key_used]:.1f}%  ({soc_key_used})")
        else:
            initial_soc_frac = FALLBACK_SOC
            soc_source       = "fallback"
            print(f"  SOC fallback  : {FALLBACK_SOC*100:.0f}%")

        # ── Block 3: Weather ──────────────────────────────────────
        try:
            weather_df, weather_mode = get_weather(
                loc["lat"], loc["lon"], start_dt, end_dt
            )
            print(f"  Weather       : {weather_mode}  ({len(weather_df)} hours)")
        except Exception as e:
            print(f"  Weather fetch failed: {e} — skipping site.")
            continue

        if weather_df.empty:
            print("  No weather data — skipping.")
            continue

        # ── Block 4: GTI ──────────────────────────────────────────
        irr_df = compute_gti(
            weather_df,
            loc["lat"], loc["lon"],
            loc["tilt"], loc["azim"],
            loc["bifacial_gain"],
        )

        # ── Block 5: PV model ─────────────────────────────────────
        pv_series = simulate_pv(loc, irr_df)
        print(f"  PV peak       : {pv_series.max():.0f} W  "
              f"total: {pv_series.sum()/1000:.2f} kWh")

        # ── Block 6: Battery model ────────────────────────────────
        battery = simulate_battery(loc, pv_series, irr_df, initial_soc_frac)
        print(f"  SOC range     : {battery['soc_pct'].min():.1f}% – "
              f"{battery['soc_pct'].max():.1f}%")

        # Daily summary print
        for date_str, day in sorted(battery["daily"].items()):
            fct = _decimal_hour_to_str(day.get("full_charge_hour"))
            fct_str = f"  full charge: {fct}" if fct else "  not fully charged"
            print(f"    {date_str}: {day['gen_kwh']:.2f} kWh generated{fct_str}")

        # ── Block 7: Full-charge prediction ───────────────────────
        # Build soc_map_8am from battery sim (use simulated SOC at 08:00)
        local_idx  = battery["soc_pct"].index.tz_convert("Europe/London")
        soc_8am_map = {}
        for ts, soc_val in zip(local_idx, battery["soc_pct"].values):
            if ts.hour == 8 and ts.minute == 0:
                soc_8am_map[str(ts.date())] = float(soc_val)

        daily_fc = compute_daily_fc_inputs(irr_df, soc_8am_map)

        results.append({
            "loc":             loc,
            "battery":         battery,
            "pv_uncurtailed":  pv_series,
            "daily_fc":        daily_fc,
            "soc_source":      soc_source,
            "weather_mode":    weather_mode,
        })

    # ── Block 8: Write output ─────────────────────────────────────
    print(f"\n── Writing output ──────────────────────────────────────────")
    payload  = build_output_payload(results, generated_at)
    out_path = write_output_json(payload)

    # ── Git push (disabled by default — set AUTO_PUSH = True to enable) ──
    # When enabled, pushes ONLY docs/data/simulation_output.json to the
    # DATA_BRANCH branch, leaving all code on main completely untouched.
    AUTO_PUSH   = False
    DATA_BRANCH = "data-output"   # separate branch — main is never touched

    if AUTO_PUSH:
        if os.path.isdir(os.path.join(GITHUB_REPO_PATH, ".git")):
            _ensure_data_branch(GITHUB_REPO_PATH, DATA_BRANCH)
            git_push(GITHUB_REPO_PATH,
                     f"auto: simulation update {now_utc.strftime('%Y-%m-%d %H:%M')} UTC",
                     branch=DATA_BRANCH)
        else:
            print(f"  (No .git found at {GITHUB_REPO_PATH} — skipping push)")
    else:
        print(f"  Auto-push disabled.  JSON is at:")
        print(f"  {out_path}")
        print(f"  Set AUTO_PUSH = True in solar_simulation.py once you're ready.")

    print(f"\nAll sites complete.  Output: {out_path}")
    return payload


# ── Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    run()