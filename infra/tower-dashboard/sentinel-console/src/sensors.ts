import type { Camera, Sensor, SensorGroup, SensorLevel } from './types';
import type { StatusResponse, StreamsResponse } from './api';

// ------------------------------------------------------------------ //
// Small sensor builders                                              //
// ------------------------------------------------------------------ //

function num(
  key: string, label: string, short: string, value: number, unit: string,
  group: SensorGroup, inBar: boolean, level: SensorLevel = 'ok',
  detail?: string, max?: number, barGradient?: boolean, available = true,
): Sensor {
  return { kind: 'numeric', key, label, short, value, unit, group, inBar, level, detail, max, barGradient, available };
}

function state(
  key: string, label: string, short: string, s: string, level: SensorLevel,
  group: SensorGroup, inBar: boolean, detail?: string, available = true,
): Sensor {
  return { kind: 'state', key, label, short, state: s, level, group, inBar, detail, available };
}

function list(
  key: string, label: string, short: string, items: Array<{ label: string; up: boolean }>,
  level: SensorLevel, group: SensorGroup, inBar: boolean, detail?: string, available = true,
): Sensor {
  return { kind: 'list', key, label, short, items, level, group, inBar, detail, available };
}

/** A reading the Jetson does not have a sensor for — rendered as "—" / N/A. */
function na(key: string, label: string, short: string, group: SensorGroup, inBar = false): Sensor {
  return { kind: 'state', key, label, short, state: '—', level: 'ok', group, inBar, available: false };
}

// ------------------------------------------------------------------ //
// Live status → sensor cards                                         //
// ------------------------------------------------------------------ //

/**
 * Map a gateway status snapshot (watchdog proxy) + mediamtx stream readiness
 * into the sensor grid. Readings the tower actually reports are live; the rest
 * keep their slot in the layout as explicit N/A placeholders so the grid stays
 * complete without inventing data.
 */
export function buildSensors(
  status: StatusResponse | null,
  streams: StreamsResponse | null,
  cameras: Camera[],
): Sensor[] {
  const avail = !!status && status.available;
  const s: Sensor[] = [];

  // -- ENVIRONMENT (no weather sensors on the tower) --
  s.push(na('temp', 'Temperature', 'TEMP', 'ENVIRONMENT'));
  s.push(na('humidity', 'Humidity', 'HUMIDITY', 'ENVIRONMENT'));
  s.push(na('wind', 'Wind speed', 'WIND', 'ENVIRONMENT'));
  s.push(na('rain', 'Rainfall', 'RAIN', 'ENVIRONMENT'));

  // -- POWER (not instrumented) --
  s.push(na('battery', 'Battery', 'BATTERY', 'POWER'));
  s.push(na('solar', 'Solar input', 'SOLAR', 'POWER'));
  s.push(na('mains', 'Mains power', 'MAINS', 'POWER'));
  s.push(na('ups', 'UPS runtime', 'UPS', 'POWER'));

  // -- CONNECTIVITY (not instrumented) --
  s.push(na('uplink', 'Uplink signal', 'UPLINK', 'CONNECTIVITY'));
  s.push(na('bandwidth', 'Bandwidth', 'BW', 'CONNECTIVITY'));
  s.push(na('latency', 'Latency', 'LAT', 'CONNECTIVITY'));

  // -- SECURITY / ENCLOSURE (real GPIO + MPU) --
  const door = status?.door;
  if (avail && door && door.open != null) {
    s.push(state('door', 'Enclosure door', 'DOOR', door.open ? 'Open' : 'Closed', door.open ? 'crit' : 'ok', 'SECURITY', true, 'gpio'));
  } else {
    s.push(na('door', 'Enclosure door', 'DOOR', 'SECURITY', true));
  }

  const light = status?.light;
  if (avail && light && light.exposed != null) {
    s.push(state('cover', 'Cover / light', 'COVER', light.exposed ? 'Exposed' : 'Covered', light.exposed ? 'crit' : 'ok', 'SECURITY', true, 'light sensor'));
  } else {
    s.push(na('cover', 'Cover / light', 'COVER', 'SECURITY', true));
  }

  const impact = status?.impact;
  if (avail && impact) {
    const recent = !!impact.last_impact_utc && Date.now() - Date.parse(impact.last_impact_utc) < 120_000;
    const delta = impact.last_delta_mg != null ? impact.last_delta_mg : '—';
    const thr = impact.threshold_mg != null ? impact.threshold_mg : '—';
    s.push(state('impact', 'Impact / motion', 'IMPACT', recent ? 'Recent hit' : 'Stable', recent ? 'warn' : 'ok', 'SECURITY', true, `Δ ${delta} / ${thr} mg`));
  } else {
    s.push(na('impact', 'Impact / motion', 'IMPACT', 'SECURITY', true));
  }

  // -- CAMERAS (mediamtx readiness) --
  const ready = new Map<string, boolean>();
  (streams?.paths ?? []).forEach((p) => ready.set(p.name, !!p.ready));
  const items = cameras.map((c) => ({ label: c.path, up: ready.get(c.path) ?? false }));
  const live = items.filter((it) => it.up).length;
  const anyDown = items.some((it) => !it.up);
  const streamsAvail = !!streams && streams.available && cameras.length > 0;

  if (streamsAvail) {
    s.push(list('streams', 'Camera streams', 'STREAMS', items, anyDown ? 'crit' : 'ok', 'CAMERAS', false));
    s.push(state('feeds', 'Feeds online', 'FEEDS', `${live} / ${cameras.length}`, anyDown ? 'warn' : 'ok', 'CAMERAS', true));
  } else {
    s.push(na('streams', 'Camera streams', 'STREAMS', 'CAMERAS'));
    s.push(na('feeds', 'Feeds online', 'FEEDS', 'CAMERAS', true));
  }

  // -- SYSTEM (thermal zone, NVMe, health) --
  const temp = status?.temperature;
  if (avail && temp && temp.celsius != null) {
    const trip = temp.trigger_c ?? 80;
    const lvl: SensorLevel = temp.critical ? 'crit' : temp.celsius >= trip - 5 ? 'warn' : 'ok';
    s.push(num('thermal', 'Thermal zone', 'THERMAL', Math.round(temp.celsius * 10) / 10, '°C', 'SYSTEM', true, lvl, `${temp.zone ?? 'thermal'} · trip ${trip}°C`, trip, true));
  } else {
    s.push(na('thermal', 'Thermal zone', 'THERMAL', 'SYSTEM', true));
  }

  const disk = status?.disk;
  if (avail && disk && disk.enabled) {
    const lvl: SensorLevel = disk.faulted ? 'crit' : 'ok';
    let value = disk.faulted ? 'Fault' : 'Healthy';
    if (disk.space_free_gb != null) value = `${disk.space_free_gb} GB free`;
    const sub: string[] = [];
    if (disk.space_used_gb != null && disk.space_total_gb != null) sub.push(`${disk.space_used_gb} / ${disk.space_total_gb} GB used`);
    if (disk.percentage_used != null) sub.push(`wear ${disk.percentage_used}%`);
    if (disk.available_spare != null) sub.push(`spare ${disk.available_spare}%`);
    if (disk.smart_temp_c != null && disk.smart_temp_c !== '') sub.push(`${disk.smart_temp_c}°C`);
    s.push(state('disk', 'Disk (NVMe)', 'DISK', value, lvl, 'SYSTEM', true, sub.join(' · ') || undefined));
  } else if (avail && disk && disk.enabled === false) {
    s.push(state('disk', 'Disk (NVMe)', 'DISK', 'Disabled', 'ok', 'SYSTEM', true, 'ENABLE_NVME=0'));
  } else {
    s.push(na('disk', 'Disk (NVMe)', 'DISK', 'SYSTEM', true));
  }

  if (avail) {
    const up = fmtUptime(status?.uptime_sec ?? 0);
    const poll = status?.poll_interval_sec != null ? ` · poll ${status.poll_interval_sec}s` : '';
    s.push(state('system', 'System', 'SYSTEM', status?.mpu_present ? 'MPU ready' : 'MPU absent', 'ok', 'SYSTEM', false, `up ${up}${poll}`));
  } else {
    s.push(na('system', 'System', 'SYSTEM', 'SYSTEM'));
  }

  // recording storage % is not reported by the watchdog
  s.push(na('storage', 'Storage', 'STORAGE', 'SYSTEM'));
  s.push(na('tilt', 'Mast tilt', 'TILT', 'SYSTEM'));

  return s;
}

function fmtUptime(sec: number): string {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}
