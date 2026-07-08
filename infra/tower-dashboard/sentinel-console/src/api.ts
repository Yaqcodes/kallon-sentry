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
  error?: { code?: string; message?: string };
  [k: string]: unknown;
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as T;
}

export const getConfig = () => getJSON<ConfigResponse>('/api/config');
export const getStreams = () => getJSON<StreamsResponse>('/api/streams');
export const getStatus = () => getJSON<StatusResponse>('/api/status');

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
