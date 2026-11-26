/**
 * Classify a day as 'green' | 'amber' | 'red' for the plan week strip,
 * from the battery model's predicted full-charge time:
 *   green — battery full before 12:00
 *   amber — battery full in the afternoon
 *   red   — battery not expected to fill that day
 *
 * (Previously this also tested daily_gti_kwh < 1.5, but that field is always
 * null, and null < 1.5 is true in JavaScript, so every day came out red.)
 */
export function classifyDay(prediction) {
  if (!prediction?.predicted_full_charge_time) return 'red';
  const [h] = prediction.predicted_full_charge_time.split(':').map(Number);
  if (!Number.isFinite(h)) return 'red';
  return h < 12 ? 'green' : 'amber';
}

/**
 * Get the weather icon emoji for a given weather_icon string.
 */
export function weatherEmoji(icon) {
  const map = {
    sunny: '☀️',
    clear: '☀️',
    partly_cloudy: '⛅',
    cloudy: '🌥️',
    overcast: '☁️',
    rainy: '🌧️',
    stormy: '⛈️',
  };
  return map[icon] || '🌤️';
}

/**
 * Plain-English weather label.
 */
export function weatherLabel(icon) {
  const map = {
    sunny: 'Sunny',
    clear: 'Clear',
    partly_cloudy: 'Partly Cloudy',
    cloudy: 'Cloudy',
    overcast: 'Overcast',
    rainy: 'Rainy',
    stormy: 'Stormy',
  };
  return map[icon] || 'Variable';
}
