// Thin client for the on-Jetson gateway (gateway.py), served on the same
// loopback origin as this SPA. Every call is same-origin; there is no hub/cloud
// API in the picture — the dashboard talks to the tower directly.

export interface GwCamera {
  camera: number;
  path: string;
  label: string;
  ip: string;
  hls_url: string;
  mjpeg_url: string | null;
}

export interface ConfigResponse {
  device_id: string;
  cameras: GwCamera[];
  hls_base: string;
}

export interface StreamPath {
  name: string;
  ready: boolean;
  readers: number;
  source: string | null;
}

export interface StreamsResponse {
  available: boolean;
  error?: string;
  paths: StreamPath[];
}

export interface StatusResponse {
  available: boolean;
  error?: string;
  device_id?: string;
  poll_interval_sec?: number;
  mpu_present?: boolean;
  uptime_sec?: number;
  timestamp_utc?: string;
  door?: { open: boolean | null };
  light?: { exposed: boolean | null };
  impact?: {
    threshold_mg?: number | null;
    last_delta_mg?: number | null;
    last_impact_utc?: string | null;
  };
  temperature?: {
    celsius?: number | null;
    zone?: string | null;
    critical?: boolean;
    trigger_c?: number;
    clear_c?: number;
  };
  disk?: {
    enabled?: boolean;
    faulted?: boolean;
    space_free_gb?: number | null;
    space_total_gb?: number | null;
    space_used_gb?: number | null;
    percentage_used?: number | null;
    available_spare?: number | null;
    smart_temp_c?: number | string | null;
  };
  streams?: Array<{ path: string; ok: boolean }>;
}

/** Raw alert as emitted by the gateway SSE stream (normalized watchdog alert). */
export interface GwAlert {
  device_id?: string;
  alert_type: string;
  kind?: string;
  severity: string; // 'info' | 'warning' | 'critical'
  timestamp_utc?: string | null;
  received_utc?: string | null;
  nonce?: string | null;
  details?: Record<string, unknown>;
}

export interface PtzResult {
  ok?: boolean;
  result?: { pan?: number; tilt?: number; zoom?: number; [k: string]: unknown };
  error?: { code?: string; message?: string };
  [k: string]: unknown;
}

export interface SnapshotResult {
  ok: boolean;
  path?: string;
  filename?: string;
  error?: { code?: string; message?: string };
}

export interface RecordingPathStatus {
  name: string;
  record: boolean | null;
  ready: boolean | null;
}

export interface RecordingStatus {
  enabled: boolean;
  desired?: boolean | null;
  effective?: boolean | null;
  record_path?: string;
  delete_after?: string;
  segment_duration?: string;
  paths?: RecordingPathStatus[];
  disk?: {
    mount?: string;
    source?: string | null;
    on_nvme?: boolean | null;
    space_free_gb?: number;
    space_total_gb?: number;
    space_used_gb?: number;
  };
  warnings?: string[];
  ok?: boolean;
  persist_ok?: boolean;
  persist_error?: string;
  error?: { code?: string; message?: string };
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as T;
}

export const getConfig = () => getJSON<ConfigResponse>('/api/config');
export const getStreams = () => getJSON<StreamsResponse>('/api/streams');
export const getStatus = () => getJSON<StatusResponse>('/api/status');
export const getRecording = () => getJSON<RecordingStatus>('/api/recording');

export interface LocalRecordingSegment {
  camera: number;
  filename: string;
  rel_path: string;
  size_bytes: number;
  mtime_utc: string;
  playback_url: string;
}

export interface LocalRecordingsResponse {
  record_path: string;
  segments: LocalRecordingSegment[];
  upload_enable?: boolean;
  delete_after_configured?: string;
  delete_after_effective?: string;
  segment_duration?: string;
  error?: string;
}

export function getLocalRecordings(params: { camera?: number; limit?: number } = {}): Promise<LocalRecordingsResponse> {
  const q = new URLSearchParams();
  if (params.camera != null) q.set('camera', String(params.camera));
  if (params.limit != null) q.set('limit', String(params.limit));
  const qs = q.toString();
  return getJSON(`/api/recordings${qs ? `?${qs}` : ''}`);
}

/** Toggle continuous NVR recording on all cameras (global). */
export async function setRecording(enabled: boolean): Promise<RecordingStatus> {
  try {
    const r = await fetch('/api/recording', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    const body = (await r.json()) as RecordingStatus;
    if (!r.ok) {
      return {
        enabled,
        ok: false,
        error: body.error ?? { code: 'HTTP', message: `HTTP ${r.status}` },
        warnings: body.warnings,
      };
    }
    return body;
  } catch (e) {
    return { enabled, ok: false, error: { code: 'GATEWAY', message: String(e) } };
  }
}
/** Relay a single PTZ command to the daemon via the gateway. */
export async function ptz(method: string, params: Record<string, unknown>): Promise<PtzResult> {
  try {
    const r = await fetch('/api/ptz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method, params }),
    });
    return (await r.json()) as PtzResult;
  } catch (e) {
    return { ok: false, error: { code: 'GATEWAY', message: String(e) } };
  }
}

/** Grab one JPEG from the local rebroadcast; saved server-side by the gateway. */
export async function snapshot(camera: number): Promise<SnapshotResult> {
  try {
    const r = await fetch('/api/snapshot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera }),
    });
    return (await r.json()) as SnapshotResult;
  } catch (e) {
    return { ok: false, error: { code: 'GATEWAY', message: String(e) } };
  }
}
