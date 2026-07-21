import { useEffect, useRef, useState } from 'react';
import type { AlertEvent, AlertLevel } from './types';
import {
  type ConfigResponse, type GwAlert, type StatusResponse, type StreamsResponse,
} from './api';
import { formatDateTimeUTC1 } from '../clock';

const STREAMS_POLL_MS = 3000;
const STREAMS_POLL_HIDDEN_MS = 8000;
const STATUS_POLL_MS = 2000;
const STATUS_POLL_HIDDEN_MS = 6000;
const CONFIG_RETRY_MS = 4000;
const MAX_ALERTS = 200;
const FETCH_TIMEOUT_MS = 8000;

function severityLevel(sev: string): AlertLevel {
  const s = (sev || '').toLowerCase();
  if (s === 'critical' || s === 'bad' || s === 'error') return 'bad';
  if (s === 'warning' || s === 'warn') return 'warn';
  return 'good';
}

function alertKey(a: GwAlert): string {
  return a.nonce || `${a.alert_type}|${a.timestamp_utc || ''}|${a.received_utc || ''}`;
}

function toAlertEvent(a: GwAlert, key: string): AlertEvent {
  const timestampUtc = a.timestamp_utc ?? a.received_utc ?? null;
  return {
    id: key,
    type: a.alert_type,
    level: severityLevel(a.severity),
    timestampUtc,
    time: timestampUtc ? formatDateTimeUTC1(timestampUtc) : '',
    payload: a.details ?? {},
  };
}

async function fetchJSON<T>(url: string): Promise<T> {
  const ac = new AbortController();
  const timer = window.setTimeout(() => ac.abort(), FETCH_TIMEOUT_MS);
  try {
    const r = await fetch(url, { headers: { Accept: 'application/json' }, signal: ac.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

export interface TowerData {
  config: ConfigResponse | null;
  streams: StreamsResponse | null;
  status: StatusResponse | null;
  alerts: AlertEvent[];
  /** gateway reachable (last status poll succeeded) */
  connected: boolean;
}

/**
 * Owns all reads from the local gateway: one-shot config, polled stream
 * readiness + health status, and the live SSE alert feed. Everything is
 * same-origin loopback; nothing here talks to a hub.
 */
export function useTower(): TowerData {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [streams, setStreams] = useState<StreamsResponse | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden');
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    const onVis = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, []);

  // config: load once, retry until the gateway answers
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const cfg = await fetchJSON<ConfigResponse>('/api/config');
        if (!cancelled) setConfig(cfg);
      } catch {
        if (!cancelled) timer = window.setTimeout(load, CONFIG_RETRY_MS);
      }
    };
    load();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, []);

  // stream readiness poll
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const interval = visible ? STREAMS_POLL_MS : STREAMS_POLL_HIDDEN_MS;
    const tick = async () => {
      try {
        const data = await fetchJSON<StreamsResponse>('/api/streams');
        if (!cancelled) setStreams(data);
      } catch {
        if (!cancelled) setStreams((prev) => prev ?? { available: false, paths: [] });
      }
      if (!cancelled) timer = window.setTimeout(tick, interval);
    };
    tick();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [visible]);

  // health status poll (also drives the connection indicator)
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const interval = visible ? STATUS_POLL_MS : STATUS_POLL_HIDDEN_MS;
    const tick = async () => {
      try {
        const data = await fetchJSON<StatusResponse>('/api/status');
        if (cancelled) return;
        setStatus(data);
        setConnected(true);
      } catch {
        if (cancelled) return;
        setStatus({ available: false });
        setConnected(false);
      }
      if (!cancelled) timer = window.setTimeout(tick, interval);
    };
    tick();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [visible]);

  // live alerts via Server-Sent Events (explicit reconnect on drop)
  useEffect(() => {
    let cancelled = false;
    let es: EventSource | undefined;
    let retry: number | undefined;

    const connect = () => {
      if (cancelled) return;
      es = new EventSource('/api/events');
      es.onmessage = (ev) => {
        try {
          const raw = JSON.parse(ev.data) as GwAlert;
          const key = alertKey(raw);
          if (seen.current.has(key)) return;
          seen.current.add(key);
          if (seen.current.size > 500) {
            seen.current = new Set(Array.from(seen.current).slice(-300));
          }
          setAlerts((prev) => [toAlertEvent(raw, key), ...prev].slice(0, MAX_ALERTS));
        } catch { /* ignore malformed frame */ }
      };
      es.onerror = () => {
        es?.close();
        if (!cancelled) retry = window.setTimeout(connect, 3000);
      };
    };

    connect();
    return () => {
      cancelled = true;
      if (retry) window.clearTimeout(retry);
      es?.close();
    };
  }, []);

  return { config, streams, status, alerts, connected };
}
