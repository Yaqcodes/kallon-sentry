// UI model types for the single-tower Sentinel console. This dashboard runs on
// the Jetson itself and talks only to the local gateway (loopback); there is no
// fleet/hub concept here — one instance = this one tower.

export type CameraStatus = 'ONLINE' | 'STANDBY' | 'OFFLINE';

export interface Camera {
  /** Two-digit camera id within the tower, e.g. "01" (from gateway camera N) */
  id: string;
  /** mediamtx path name, e.g. "cam1" — used to match stream readiness */
  path: string;
  /** Human label, e.g. "CAM 01" */
  label: string;
  status: CameraStatus;

  /** MJPEG proxy URL (preferred) and HLS URL (fallback) from the gateway. */
  mjpegUrl?: string;
  hlsUrl?: string;

  /**
   * Live PTZ readout from ONVIF GetStatus (polled via the gateway).
   * ptzLive is false until the first successful poll for this camera.
   */
  az: number;
  el: number;
  zoom: number;
  ptzLive: boolean;

  recording: boolean;
  recStart: number | null;

  homeAz: number;
  homeEl: number;
}

/** Severity of a single sensor reading (semantic, not the mint accent). */
export type SensorLevel = 'ok' | 'warn' | 'crit';

export type SensorGroup =
  | 'ENVIRONMENT'
  | 'POWER'
  | 'CONNECTIVITY'
  | 'CAMERAS'
  | 'SECURITY'
  | 'SYSTEM';

interface SensorBase {
  key: string;
  label: string;
  short: string;
  level: SensorLevel;
  group: SensorGroup;
  inBar: boolean;
  detail?: string;
  /**
   * False when this tower has no sensor for this reading (the Jetson watchdog
   * does not report it). The card/chip renders as "—" / N/A and is excluded
   * from the rolled-up health so placeholders never trip an alert state.
   */
  available?: boolean;
}

export interface NumericSensor extends SensorBase {
  kind: 'numeric';
  value: number;
  unit: string;
  max?: number;
  barGradient?: boolean;
}

export interface StateSensor extends SensorBase {
  kind: 'state';
  state: string;
}

export interface ListSensor extends SensorBase {
  kind: 'list';
  items: Array<{ label: string; up: boolean }>;
}

export type Sensor = NumericSensor | StateSensor | ListSensor;

/** Severity of a live-alert event. */
export type AlertLevel = 'good' | 'warn' | 'bad';

export interface AlertEvent {
  id: string;
  type: string;
  level: AlertLevel;
  time: string; // 'HH:MM:SS'
  payload: Record<string, unknown>;
}
