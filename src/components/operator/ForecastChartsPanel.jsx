/**
 * ForecastChartsPanel.jsx
 * Fixes:
 *  - Load simulation keyed by date:hour (not just hour), so loads only
 *    affect the specific day they're scheduled on
 *  - SOC simulation resets to 50% at start of day 3+ (matching Python)
 *  - 50% reset reference line positioned correctly
 */
import { useState, useMemo, useCallback } from 'react';
import {
  ComposedChart, Line, Bar, XAxis, YAxis,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from 'recharts';
import { useSimulator } from '../../context/useSimulator';

const C = {
  pv:      '#f5a623',
  soc:     '#00d4aa',
  socLoad: '#e05c5c',
  dim:     '#475569',
};

const HOURS = Array.from({ length: 19 }, (_, i) => i + 5); // 05–23

function utcHour(iso)  { return new Date(iso).getUTCHours(); }
function utcLabel(iso) {
  const d = new Date(iso);
  return `${String(d.getUTCHours()).padStart(2,'0')}:00`;
}

function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-[#0f1a18] border border-white/10 rounded-xl px-3 py-2 text-xs shadow-xl min-w-[130px]">
      <p className="text-slate-400 mb-1">{label}</p>
      {payload.map((p) => p.value != null && (
        <p key={p.dataKey} style={{ color: p.color }} className="font-semibold">
          {p.name}: {Number(p.value).toFixed(1)}{p.unit ?? ''}
        </p>
      ))}
    </div>
  );
}

/**
 * Compute SOC-with-loads by subtracting load drain from the Python SOC baseline.
 *
 * KEY PRINCIPLE: The Python SOC already has PV charging baked in.
 * We must NOT re-add PV charging here — that causes double-counting
 * which makes SOC go UP when loads are added (the bug we observed).
 *
 * We only compute HOW MUCH the battery is drained DIFFERENTLY because
 * of the scheduled loads, then subtract that cumulative delta from the
 * Python SOC at each hour.
 *
 * How a scheduled load changes the battery, hour by hour:
 *
 *   spare PV = pv_available - pv_power_w  (solar Python curtailed because the
 *              battery was full; zero while charging or at night)
 *
 *   1. The load uses spare PV first — that costs the battery nothing
 *      (the EPEVER "free energy" state when the battery is full).
 *   2. Whatever spare PV can't cover comes out of the battery (added to the deficit).
 *   3. Spare PV left over refills any deficit from earlier loads.
 *
 * SOC is % of TOTAL capacity (EPEVER convention), so the deficit is divided by
 * the total battery capacity, not the usable part.
 */
function simulateWithLoads(rows, scheduledWByKey, totalCapWh, day3Date) {
  if (!rows.length || !totalCapWh) return { socArr: [], pvUsedArr: [] };

  const socArr    = [];
  const pvUsedArr = [];

  // Cumulative battery deficit caused by scheduled loads (Wh)
  let cumulativeDrainWh = 0;
  let curDay = rows[0]?.date ?? '';

  for (const row of rows) {
    if (row.date !== curDay) {
      curDay = row.date;
      // Python resets SOC to a fixed value from day 3, so earlier loads stop mattering
      if (row.date >= day3Date) cumulativeDrainWh = 0;
    }

    const key       = `${row.date}:${utcHour(row.timestamp)}`;
    const loadW     = scheduledWByKey[key] ?? 0;
    const usedW     = row.pv_power_w ?? 0;
    const spareW    = Math.max(0, (row.pv_available_w ?? usedW) - usedW);
    const pythonSoc = row.soc ?? 50;

    const fromSpare = Math.min(loadW, spareW);
    cumulativeDrainWh += loadW - fromSpare;                  // battery pays the rest
    cumulativeDrainWh  = Math.max(0, cumulativeDrainWh - (spareW - fromSpare)); // refill

    const drainPct    = (cumulativeDrainWh / totalCapWh) * 100;
    const socWithLoad = Math.max(0, Math.min(100, pythonSoc - drainPct));

    socArr.push(Math.round(socWithLoad * 10) / 10);

    pvUsedArr.push(0);  // pvUsedArr unused — chart data computes pv splits directly
  }

  return { socArr, pvUsedArr };
}



export default function ForecastChartsPanel() {
  const { siteData } = useSimulator();
  const { available_dates, daily_data, appliances = [] } = siteData;

  const [viewMode, setViewMode] = useState('7day');
  const [showPV,   setShowPV]   = useState(true);
  const [showSOC,  setShowSOC]  = useState(true);

  // scheduled: { "YYYY-MM-DD:H": totalW } — only one load per slot for simplicity
  // We store as array so multiple appliances can stack in same slot
  // { "2026-03-19:9": [{ id, name, watts, icon, instanceId }] }
  const [scheduled, setScheduled] = useState({});

  const [dragging,      setDragging]      = useState(null);
  const [dragOver,      setDragOver]      = useState(null);
  const [dropTargetDate, setDropTargetDate] = useState(null);  // null = today

  const today = useMemo(() => new Date().toISOString().slice(0, 10), []);

  const day3Date = useMemo(() => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() + 2);
    return d.toISOString().slice(0, 10);
  }, []);

  const effectiveDropDate = dropTargetDate ?? today;

  // Total battery capacity (Wh) — SOC % is a share of total, EPEVER convention
  const totalCapWh = siteData?.physicsConstants?.batteryCapacityWh ?? 9600;

  // Collapse scheduled to { "date:hour": totalW } for chart sim
  const scheduledWByKey = useMemo(() => {
    const out = {};
    for (const [key, loads] of Object.entries(scheduled)) {
      out[key] = loads.reduce((s, l) => s + l.watts, 0);
    }
    return out;
  }, [scheduled]);

  const hasLoads = Object.keys(scheduled).length > 0;

  // All rows flat
  const allRows = useMemo(() => {
    const dates = viewMode === 'today'
      ? available_dates.filter((d) => d === today)
      : available_dates;
    return dates.flatMap((date) =>
      (daily_data[date]?.hourly ?? []).map((row) => ({ ...row, date }))
    );
  }, [available_dates, daily_data, today, viewMode]);

  // SOC with loads + PV usage breakdown
  const { socWithLoad, pvUsedWithLoad } = useMemo(() => {
    if (!hasLoads) {
      return { socWithLoad: [], pvUsedWithLoad: [] };
    }
    const { socArr, pvUsedArr } = simulateWithLoads(
      allRows, scheduledWByKey, totalCapWh, day3Date
    );
    return { socWithLoad: socArr, pvUsedWithLoad: pvUsedArr };
  }, [allRows, scheduledWByKey, totalCapWh, day3Date, hasLoads]);

  // Chart data — simple stacked bar logic:
  //   pv_bright: PV that is DOING something useful (charging battery OR serving load) → bright
  //   pv_faint:  PV that is wasted / curtailed (battery full, no load) → faint
  //   Total bar height = pv_available (what the panels could deliver)
  //
  // Python now models curtailment: pv_w is the PV actually used (load + charging)
  // and pv_available is the uncurtailed maximum. The gap is curtailed PV.
  // A scheduled load unlocks curtailed PV up to its own size: that part turns bright.
  const chartData = useMemo(() =>
    allRows.map((row, i) => {
      const usedW     = row.pv_power_w ?? 0;
      const availW    = Math.max(usedW, row.pv_available_w ?? usedW);
      const pythonSoc = row.soc ?? 50;
      const key       = `${row.date}:${utcHour(row.timestamp)}`;
      const loadW     = scheduledWByKey[key] ?? 0;

      const spareW    = availW - usedW;             // curtailed by Python
      const unlockedW = Math.min(spareW, loadW);    // a scheduled load uses some of it
      const pv_bright = usedW + unlockedW;
      const pv_faint  = spareW - unlockedW;

      return {
        label: viewMode === 'today'
          ? utcLabel(row.timestamp)
          : `${row.date.slice(5)} ${utcLabel(row.timestamp)}`,
        date:          row.date,
        hour:          utcHour(row.timestamp),
        pv_bright,
        pv_faint,
        soc:           pythonSoc,
        soc_with_load: hasLoads ? (socWithLoad[i] ?? null) : null,
        isDay3:        row.date >= day3Date,
      };
    }),
  [allRows, viewMode, socWithLoad, hasLoads, day3Date, scheduledWByKey]);

  // Day boundary labels
  const dayBoundaries = useMemo(() => {
    if (viewMode !== '7day') return [];
    const seen = new Set();
    return chartData
      .filter((p) => { if (seen.has(p.date)) return false; seen.add(p.date); return true; })
      .map((p) => ({ label: p.label, isDay3: p.isDay3 }));
  }, [chartData, viewMode]);

  // First day3 label for reference line
  const day3Label = useMemo(() =>
    chartData.find((p) => p.isDay3)?.label ?? null,
  [chartData]);

  const tickInterval = viewMode === '7day'
    ? Math.max(1, Math.floor(chartData.length / 10))
    : 3;

  // Drag handlers
  const onDragStart  = useCallback((a) => setDragging(a), []);
  const onDragOver   = useCallback((e, key) => { e.preventDefault(); setDragOver(key); }, []);
  const onDragLeave  = useCallback(() => setDragOver(null), []);

  const onDrop = useCallback((e, date, hour) => {
    e.preventDefault();
    if (!dragging) return;
    const key = `${date}:${hour}`;
    setScheduled((prev) => ({
      ...prev,
      [key]: [...(prev[key] ?? []), { ...dragging, instanceId: `${dragging.id}-${Date.now()}` }],
    }));
    setDragging(null);
    setDragOver(null);
  }, [dragging]);

  const removeLoad = useCallback((key, instanceId) => {
    setScheduled((prev) => {
      const updated = (prev[key] ?? []).filter((l) => l.instanceId !== instanceId);
      if (!updated.length) {
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return { ...prev, [key]: updated };
    });
  }, []);

  const totalLoadWh = useMemo(() =>
    Object.values(scheduled).flat()
      .reduce((s, l) => s + l.watts * ((l.durationMinutes ?? 60) / 60), 0),
  [scheduled]);

  const selectableAppliances = appliances.filter((a) => a.userSelectable !== false);

  return (
    <div className="flex flex-col gap-5">

      {/* Header + view toggle */}
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider">
          Forecast Charts
        </h3>
        <div className="flex bg-[#0d1a17] border border-white/10 rounded-lg overflow-hidden text-xs">
          {[['today','Today'],['7day','7 Days']].map(([val,lbl]) => (
            <button key={val} onClick={() => setViewMode(val)}
              className={`px-3 py-1.5 transition-colors ${
                viewMode === val ? 'bg-teal-500/20 text-teal-400' : 'text-slate-400 hover:text-white'
              }`}>{lbl}</button>
          ))}
        </div>
      </div>

      {/* Series toggles */}
      <div className="flex gap-2 flex-wrap items-center">
        <button onClick={() => setShowPV((v) => !v)}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all ${
            showPV ? 'bg-amber-500/15 border-amber-500/40 text-amber-400' : 'bg-surface border-white/10 text-slate-500'
          }`}>
          <span className="w-2 h-2 rounded-full" style={{ background: showPV ? C.pv : '#475569' }} />
          PV Power
        </button>
        <button onClick={() => setShowSOC((v) => !v)}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all ${
            showSOC ? 'bg-teal-500/15 border-teal-500/40 text-teal-400' : 'bg-surface border-white/10 text-slate-500'
          }`}>
          <span className="w-2 h-2 rounded-full" style={{ background: showSOC ? C.soc : '#475569' }} />
          Battery SOC
        </button>
        {hasLoads && (
          <button onClick={() => setScheduled({})}
            className="ml-auto flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs border border-red-500/30 text-red-400 bg-red-500/10 hover:bg-red-500/20 transition-colors">
            ✕ Clear loads
          </button>
        )}
      </div>

      {/* Main chart */}
      <div className="bg-[#0d1a17] rounded-2xl p-3 border border-white/5">
        <ResponsiveContainer width="100%" height={220}>
          <ComposedChart data={chartData} margin={{ top: 5, right: 8, left: -15, bottom: 0 }}>
            <XAxis dataKey="label"
              tick={{ fill: C.dim, fontSize: 9 }}
              axisLine={false} tickLine={false}
              interval={tickInterval}
              angle={viewMode === '7day' ? -35 : 0}
              textAnchor={viewMode === '7day' ? 'end' : 'middle'}
              height={viewMode === '7day' ? 38 : 18} />
            <YAxis yAxisId="soc" orientation="left"
              tick={{ fill: C.dim, fontSize: 9 }} axisLine={false} tickLine={false}
              tickFormatter={(v) => `${v}%`} domain={[0, 100]} />
            <YAxis yAxisId="pv" orientation="right" hide={!showPV}
              tick={{ fill: C.dim, fontSize: 9 }} axisLine={false} tickLine={false}
              tickFormatter={(v) => `${v}W`} domain={[0, 'auto']} />
            <Tooltip content={<ChartTooltip />} />

            {/* Day boundary lines */}
            {viewMode === '7day' && dayBoundaries.map(({ label }) => (
              <ReferenceLine key={label} x={label} yAxisId="soc"
                stroke="rgba(255,255,255,0.07)" strokeDasharray="4 2" />
            ))}

            {/* Day 3+ SOC reset marker */}
            {viewMode === '7day' && day3Label && (
              <ReferenceLine x={day3Label} yAxisId="soc"
                stroke="rgba(245,166,35,0.45)" strokeDasharray="6 3"
                label={{
                  value: '← 50% reset',
                  fill: 'rgba(245,166,35,0.6)',
                  fontSize: 9,
                  position: 'insideTopRight',
                }} />
            )}

            {showPV && (
              <>
                {/* Bright bottom: PV doing useful work (charging or serving load) */}
                <Bar yAxisId="pv" dataKey="pv_bright" name="PV active" unit="W"
                  stackId="pv" fill={C.pv} opacity={0.85} maxBarSize={10} />
                {/* Faint top: PV wasted / curtailed (battery full, no load) */}
                <Bar yAxisId="pv" dataKey="pv_faint" name="PV curtailed" unit="W"
                  stackId="pv" fill={C.pv} opacity={0.15} maxBarSize={10} />
              </>
            )}
            {showSOC && (
              <Line yAxisId="soc" type="monotone" dataKey="soc"
                name="SOC" unit="%" stroke={C.soc} strokeWidth={2}
                dot={false} activeDot={{ r: 3, fill: C.soc, strokeWidth: 0 }} />
            )}
            {hasLoads && (
              <Line yAxisId="soc" type="monotone" dataKey="soc_with_load"
                name="SOC + loads" unit="%" stroke={C.socLoad}
                strokeWidth={2} strokeDasharray="5 3"
                dot={false} activeDot={{ r: 3, fill: C.socLoad, strokeWidth: 0 }} />
            )}
          </ComposedChart>
        </ResponsiveContainer>

        {hasLoads && (
          <p className="text-xs text-center mt-1" style={{ color: C.socLoad + 'aa' }}>
            — — SOC with scheduled loads ({totalLoadWh.toFixed(0)} Wh)
          </p>
        )}
      </div>

      {/* Load scheduler */}
      <div>
        <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
          <h4 className="text-xs font-semibold text-slate-500 uppercase tracking-wider">
            Schedule Loads — drag onto a time slot
          </h4>
          {viewMode === '7day' && (
            <div className="flex gap-1 flex-wrap">
              {available_dates.slice(0, 7).map((d) => {
                const isActive = (dropTargetDate ?? today) === d;
                const label = new Date(d + 'T12:00:00Z')
                  .toLocaleDateString('en-GB', { weekday: 'short' });
                return (
                  <button key={d} onClick={() => setDropTargetDate(d)}
                    className={`px-2 py-1 rounded text-xs border transition-colors ${
                      isActive
                        ? 'border-teal-500/50 bg-teal-500/15 text-teal-400'
                        : 'border-white/10 text-slate-500 hover:text-white'
                    }`}>{label}</button>
                );
              })}
            </div>
          )}
        </div>

        {/* Appliance palette */}
        <div className="flex flex-wrap gap-2 mb-3">
          {selectableAppliances.map((a) => (
            <div key={a.id} draggable
              onDragStart={() => onDragStart(a)}
              className="flex items-center gap-2 px-3 py-2 rounded-xl text-xs border border-white/10 bg-surface text-slate-300 cursor-grab active:cursor-grabbing hover:border-teal-500/40 hover:text-white transition-all select-none"
            >
              <span className="text-base leading-none">{a.icon}</span>
              <div>
                <p className="font-medium leading-tight">{a.name}</p>
                <p className="text-slate-500">{a.watts}W · {a.durationMinutes ?? 60}min</p>
              </div>
            </div>
          ))}
        </div>

        {/* Hour bins */}
        <p className="text-xs text-slate-600 mb-1">
          Dropping onto:{' '}
          <span className="text-slate-400">
            {new Date(effectiveDropDate + 'T12:00:00Z')
              .toLocaleDateString('en-GB', { weekday:'long', day:'numeric', month:'short' })}
          </span>
        </p>

        <div className="overflow-x-auto pb-2">
          <div className="flex gap-1.5 min-w-max">
            {HOURS.map((hour) => {
              const key   = `${effectiveDropDate}:${hour}`;
              const over  = dragOver === key;
              const loads = scheduled[key] ?? [];

              return (
                <div key={hour}
                  onDragOver={(e) => onDragOver(e, key)}
                  onDragLeave={onDragLeave}
                  onDrop={(e) => onDrop(e, effectiveDropDate, hour)}
                  className={`flex flex-col items-center rounded-xl border transition-all min-w-[52px] p-1.5 ${
                    over
                      ? 'border-teal-400/70 bg-teal-500/20'
                      : loads.length
                        ? 'border-red-500/40 bg-red-500/10'
                        : 'border-white/8 bg-surface hover:border-white/20'
                  }`}
                >
                  <span className="text-xs text-slate-500 mb-1">
                    {String(hour).padStart(2,'0')}:00
                  </span>
                  {loads.length > 0 ? (
                    <div className="flex flex-col gap-1 w-full">
                      {loads.map((l) => (
                        <button key={l.instanceId}
                          onClick={() => removeLoad(key, l.instanceId)}
                          title="Click to remove"
                          className="text-xs bg-red-500/20 border border-red-500/30 rounded-lg px-1 py-0.5 text-red-300 hover:bg-red-500/40 transition-colors leading-tight text-center w-full"
                        >
                          {l.icon} {l.watts}W
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className={`w-full h-7 rounded-lg border border-dashed flex items-center justify-center text-xs ${
                      over ? 'border-teal-400/60 text-teal-400' : 'border-white/10 text-slate-700'
                    }`}>
                      {over ? '↓' : '+'}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <p className="text-xs text-slate-600 mt-1.5 text-center">
          Drag appliances onto hour slots · click a scheduled item to remove it
        </p>
      </div>

      {viewMode === '7day' && day3Label && (
        <p className="text-xs text-amber-500/50 text-center">
          ⚠ Days 3–7 assume 50% SOC at midnight — worst-case planning estimate
        </p>
      )}
    </div>
  );
}