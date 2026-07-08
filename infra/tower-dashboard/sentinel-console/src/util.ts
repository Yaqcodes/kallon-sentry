import { colors } from './tokens';
import type { AlertLevel, Sensor, SensorLevel } from './types';

const RANK: Record<SensorLevel, number> = { ok: 0, warn: 1, crit: 2 };

/** Worst level across the sensors that are actually available on this tower. */
export function worstLevel(sensors: Sensor[]): SensorLevel {
  return sensors.reduce<SensorLevel>(
    (w, s) => (s.available === false ? w : RANK[s.level] > RANK[w] ? s.level : w),
    'ok',
  );
}

/** Worst sensor state + a short label for the header health chip. */
export function health(sensors: Sensor[]): { level: SensorLevel; label: string } {
  let worst: SensorLevel = 'ok';
  let ws: Sensor | undefined;
  for (const s of sensors) {
    if (s.available === false) continue;
    if (RANK[s.level] > RANK[worst]) { worst = s.level; ws = s; }
  }
  if (worst === 'ok' || !ws) return { level: 'ok', label: 'OK' };
  const label = ws.kind === 'numeric' ? `${ws.short} ${ws.value}${ws.unit}` : ws.short;
  return { level: worst, label };
}

export function levelColor(level: SensorLevel): string {
  return level === 'crit' ? colors.offline : level === 'warn' ? colors.standby : colors.accent;
}

export function alertColor(level: AlertLevel): string {
  return level === 'bad' ? colors.offline : level === 'warn' ? colors.standby : colors.accent;
}
