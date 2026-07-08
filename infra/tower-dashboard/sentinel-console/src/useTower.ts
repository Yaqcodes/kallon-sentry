import { useEffect, useRef, useState } from 'react';
import type { AlertEvent, AlertLevel } from './types';
import {
  getConfig, getStatus, getStreams,
  type ConfigResponse, type GwAlert, type StatusResponse, type StreamsResponse,
} from './api';

const STREAMS_POLL_MS = 3000;
const STATUS_POLL_MS = 2000;
const CONFIG_RETRY_MS = 4000;
const MAX_ALERTS = 200;

function severityLevel(sev: string): AlertLevel {
  const s = (sev || '').toLowerCase();
  if (s === 'critical' || s === 'bad' || s === 'error') return 'bad';
  if (s === 'warning' || s === 'warn') return 'warn';
  return 'good';
}

function fmtClock(iso?: string | null): string {
  if (!iso) return '';
  try { return new Date(iso).toLocaleTimeString([], { hour12: false }); } catch { return iso; }
}

function alertKey(a: GwAlert): string {
  return a.nonce || `${a.alert_type}|${a.timestamp_utc || ''}|${a.received_utc || ''}`;
}

function toAlertEvent(a: GwAlert, key: string): AlertEvent {
  return {
    id: key,
    type: a.alert_type,
    level: severityLevel(a.severity),
    time: fmtClock(a.timestamp_utc || a.received_utc),
    payload: a.details ?? {},
  };
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
  const seen = useRef<Set<string>>(new Set());

  // config: load once, retry until the gateway answers
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const cfg = await getConfig();
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
    const tick = async () => {
      try {
        const data = await getStreams();
        if (!cancelled) setStreams(data);
      } catch {
        if (!cancelled) setStreams({ available: false, paths: [] });
      }
    };
    tick();
    const id = window.setInterval(tick, STREAMS_POLL_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  // health status poll (also drives the connection indicator)
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const data = await getStatus();
        if (cancelled) return;
        setStatus(data);
        setConnected(true);
      } catch {
        if (cancelled) return;
        setStatus({ available: false });
        setConnected(false);
      }
    };
    tick();
    const id = window.setInterval(tick, STATUS_POLL_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  // live alerts via Server-Sent Events (auto-reconnecting)
  useEffect(() => {
    const es = new EventSource('/api/events');
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
    return () => es.close();
  }, []);

  return { config, streams, status, alerts, connected };
}
