import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { Camera, CameraStatus } from '../types';
import { colors, font } from '../tokens';
import { health, levelColor } from '../util';
import { buildSensors } from '../sensors';
import { ptz, snapshot } from '../api';
import { useTower } from '../useTower';
import TowerFeed from './TowerFeed';
import PtzPad, { type PanDir } from './PtzPad';
import PtzSpeedSlider, { speedToVelocity } from './PtzSpeedSlider';
import SensorBar from './SensorBar';
import SensorPanel from './SensorPanel';

const ACCENT = colors.accent;
const PAN_STEP = 6;
const TILT_STEP = 3;
const ZOOM_STEP = 0.3;
const PTZ_PULSE_SEC = 0.2;   // fixed tap/hold pulse — distance scales with speed slider
const PTZ_HOLD_MS = 180;     // press longer than this → jog instead of single nudge
const PTZ_JOG_MS = 280;      // repeat interval while holding pad
const PTZ_SPEED_KEY = 'sentinel-ptz-speed';

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
const pad3 = (n: number) => String(((Math.round(n) % 360) + 360) % 360).padStart(3, '0');
const elFmt = (e: number) => (e >= 0 ? '+' : '-') + String(Math.abs(Math.round(e))).padStart(2, '0');

const isFormField = (el: EventTarget | null) => {
  const t = el as HTMLElement | null;
  return !!t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
};

interface Estimate { az: number; el: number; zoom: number }

export default function SentinelConsole() {
  const { config, streams, status, alerts, connected } = useTower();

  // Operator's PTZ estimate per camera (no reliable absolute feedback exists
  // over the loopback relay, so we track our own inputs for the HUD readout).
  const [estimates, setEstimates] = useState<Record<string, Estimate>>({});
  const [selectedCamId, setSelectedCamId] = useState<string>('');
  const [now, setNow] = useState(() => Date.now());
  const [controlOpen, setControlOpen] = useState(true);
  const [spotlight, setSpotlight] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [ptzMsg, setPtzMsg] = useState('');
  const [ptzSpeedPct, setPtzSpeedPct] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(PTZ_SPEED_KEY));
      if (saved >= 5 && saved <= 100) return saved;
    } catch { /* private mode */ }
    return 35;
  });

  const ptzVelocity = useMemo(() => speedToVelocity(ptzSpeedPct), [ptzSpeedPct]);
  const jogTimer = useRef<number | undefined>(undefined);
  const jogInterval = useRef<number | undefined>(undefined);
  const jogDir = useRef<PanDir | null>(null);
  const jogging = useRef(false);

  const readyByPath = useMemo(() => {
    const m = new Map<string, boolean>();
    (streams?.paths ?? []).forEach((p) => m.set(p.name, !!p.ready));
    return m;
  }, [streams]);

  const cameras: Camera[] = useMemo(() => {
    const gw = config?.cameras ?? [];
    return gw.map((c) => {
      const id = String(c.camera).padStart(2, '0');
      const est = estimates[id] ?? { az: 0, el: 0, zoom: 1.0 };
      let cstatus: CameraStatus = 'STANDBY';
      if (streams && streams.available) cstatus = readyByPath.get(c.path) ? 'ONLINE' : 'OFFLINE';
      return {
        id,
        path: c.path,
        label: (c.label || c.path).toUpperCase(),
        status: cstatus,
        mjpegUrl: c.mjpeg_url ?? undefined,
        hlsUrl: c.hls_url || undefined,
        az: est.az, el: est.el, zoom: est.zoom,
        recording: false,
        recStart: null,
        homeAz: 0, homeEl: 0,
      };
    });
  }, [config, streams, readyByPath, estimates]);

  const deviceName = (config?.device_id || 'SENTINEL TOWER').toUpperCase();
  const selectedCam = cameras.find((c) => c.id === selectedCamId) ?? cameras[0];
  const sensors = useMemo(() => buildSensors(status, streams, cameras), [status, streams, cameras]);
  const sysHealth = health(sensors);

  // keep a valid selection as cameras (re)appear
  useEffect(() => {
    if (cameras.length && !cameras.some((c) => c.id === selectedCamId)) {
      setSelectedCamId(cameras[0].id);
    }
  }, [cameras, selectedCamId]);

  const camNum = useCallback((id: string) => parseInt(id, 10) || 1, []);

  const bumpEstimate = useCallback((id: string, fn: (e: Estimate) => Estimate) => {
    setEstimates((prev) => ({ ...prev, [id]: fn(prev[id] ?? { az: 0, el: 0, zoom: 1.0 }) }));
  }, []);

  const feedback = useCallback((res: { ok?: boolean; error?: { message?: string } }, verb: string) => {
    if (res && res.ok === false) setPtzMsg(`${verb} failed: ${res.error?.message ?? 'error'}`);
    else setPtzMsg(`${verb} ok`);
  }, []);

  const setSpeed = useCallback((pct: number) => {
    setPtzSpeedPct(pct);
    try { localStorage.setItem(PTZ_SPEED_KEY, String(pct)); } catch { /* noop */ }
  }, []);

  const bumpForDir = useCallback((id: string, dir: PanDir, zoomDelta = 0) => {
    const p = dir === 'left' ? -1 : dir === 'right' ? 1 : 0;
    const t = dir === 'up' ? 1 : dir === 'down' ? -1 : 0;
    const scale = ptzVelocity;
    bumpEstimate(id, (e) => ({
      az: (e.az + p * PAN_STEP * scale + 360) % 360,
      el: clamp(e.el + t * TILT_STEP * scale, -20, 30),
      zoom: zoomDelta ? clamp(Number((e.zoom + zoomDelta * scale).toFixed(1)), 1.0, 8.0) : e.zoom,
    }));
  }, [bumpEstimate, ptzVelocity]);

  const sendMove = useCallback((dir: PanDir | null, zoomDir: number, seconds: number) => {
    const cam = camNum(selectedCam?.id ?? '01');
    const p = dir === 'left' ? -1 : dir === 'right' ? 1 : 0;
    const t = dir === 'up' ? 1 : dir === 'down' ? -1 : 0;
    return ptz('move_continuous', {
      camera: cam,
      pan: p * ptzVelocity,
      tilt: t * ptzVelocity,
      zoom: zoomDir * ptzVelocity,
      seconds,
    });
  }, [selectedCam, camNum, ptzVelocity]);

  const stopJog = useCallback(() => {
    if (jogTimer.current !== undefined) {
      window.clearTimeout(jogTimer.current);
      jogTimer.current = undefined;
    }
    if (jogInterval.current !== undefined) {
      window.clearInterval(jogInterval.current);
      jogInterval.current = undefined;
    }
    if (jogging.current) {
      jogging.current = false;
      const cam = camNum(selectedCam?.id ?? '01');
      void ptz('stop', { camera: cam });
    }
    jogDir.current = null;
  }, [selectedCam, camNum]);

  const nudge = useCallback((dir: PanDir) => {
    void sendMove(dir, 0, PTZ_PULSE_SEC).then((r) => feedback(r, 'move'));
    if (selectedCam) bumpForDir(selectedCam.id, dir);
  }, [sendMove, feedback, selectedCam, bumpForDir]);

  const panStart = useCallback((dir: PanDir) => {
    stopJog();
    jogDir.current = dir;
    jogTimer.current = window.setTimeout(() => {
      jogTimer.current = undefined;
      jogging.current = true;
      void sendMove(dir, 0, PTZ_PULSE_SEC);
      if (selectedCam) bumpForDir(selectedCam.id, dir);
      jogInterval.current = window.setInterval(() => {
        void sendMove(dir, 0, PTZ_PULSE_SEC);
        if (selectedCam) bumpForDir(selectedCam.id, dir);
      }, PTZ_JOG_MS);
    }, PTZ_HOLD_MS);
  }, [stopJog, sendMove, selectedCam, bumpForDir]);

  const panEnd = useCallback(() => {
    if (jogTimer.current !== undefined) {
      window.clearTimeout(jogTimer.current);
      jogTimer.current = undefined;
      if (jogDir.current) nudge(jogDir.current);
      jogDir.current = null;
      return;
    }
    stopJog();
  }, [nudge, stopJog]);

  const pan = useCallback((dir: PanDir) => {
    nudge(dir);
  }, [nudge]);

  const zoomBy = useCallback((d: number) => {
    const dir = d > 0 ? 1 : -1;
    void sendMove(null, dir, PTZ_PULSE_SEC).then((r) => feedback(r, 'zoom'));
    if (selectedCam) {
      bumpEstimate(selectedCam.id, (e) => ({
        ...e,
        zoom: clamp(Number((e.zoom + d * ptzVelocity).toFixed(1)), 1.0, 8.0),
      }));
    }
  }, [sendMove, feedback, selectedCam, bumpEstimate, ptzVelocity]);

  const captureSnapshot = useCallback(async (camId: string) => {
    const cam = camNum(camId);
    const res = await snapshot(cam);
    if (!res.ok) {
      setPtzMsg(`snapshot failed: ${res.error?.message ?? 'error'}`);
      return null;
    }
    return res.path ?? null;
  }, [camNum]);

  const recenter = useCallback(() => {
    const cam = camNum(selectedCam?.id ?? '01');
    ptz('home', { camera: cam }).then((r) => feedback(r, 'home'));
    if (selectedCam) bumpEstimate(selectedCam.id, () => ({ az: 0, el: 0, zoom: 1.0 }));
  }, [selectedCam, camNum, bumpEstimate, feedback]);

  // 1 Hz clock
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => () => stopJog(), [stopJog]);

  // keyboard control
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (panelOpen || isFormField(e.target)) return;
      switch (e.key) {
        case 'ArrowUp': e.preventDefault(); pan('up'); break;
        case 'ArrowDown': e.preventDefault(); pan('down'); break;
        case 'ArrowLeft': e.preventDefault(); pan('left'); break;
        case 'ArrowRight': e.preventDefault(); pan('right'); break;
        case ' ': e.preventDefault(); recenter(); break;
        case '+': case '=': zoomBy(ZOOM_STEP); break;
        case '-': case '_': zoomBy(-ZOOM_STEP); break;
        case '1': case '2': case '3': case '4': {
          const id = '0' + e.key;
          if (cameras.some((c) => c.id === id)) setSelectedCamId(id);
          break;
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pan, zoomBy, recenter, panelOpen, cameras]);

  // Esc closes the sensor panel
  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape' && panelOpen) setPanelOpen(false); };
    window.addEventListener('keydown', onEsc);
    return () => window.removeEventListener('keydown', onEsc);
  }, [panelOpen]);

  const utc = new Date(now).toISOString().slice(11, 19);
  const loading = cameras.length === 0;

  return (
    <div className="app">
      {/* ---- top bar ---- */}
      <header className="topbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontFamily: font.display, fontWeight: 700, fontSize: 24, letterSpacing: '.24em', color: colors.textBright }}>SENTINEL</span>
          <span style={{ fontFamily: font.display, fontWeight: 500, fontSize: 14, letterSpacing: '.32em', color: colors.textFaint }}>TOWER CONTROL</span>
          <span className="device-pill" title="Device ID">
            <span className="pill-hd" style={{ background: levelColor(sysHealth.level), color: levelColor(sysHealth.level) }} />
            {deviceName}
          </span>
        </div>

        <div className="pills">
          <span className="pill">
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: connected ? ACCENT : colors.offline, boxShadow: `0 0 8px ${connected ? ACCENT : colors.offline}` }} />
            GATEWAY <b>{connected ? 'OK' : 'OFFLINE'}</b>
          </span>
        </div>

        <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
          <div style={{ fontFamily: font.mono, fontSize: 19, color: colors.textBright, letterSpacing: '.06em' }}>{utc}</div>
          <div style={{ fontFamily: font.mono, fontSize: 10, letterSpacing: '.16em', color: colors.textFaint }}>UTC · {connected ? 'LINK OK' : 'NO LINK'}</div>
        </div>
      </header>

      {/* ---- sensor context bar ---- */}
      <SensorBar sensors={sensors} deviceName={deviceName} connected={connected} onOpenDetail={() => setPanelOpen(true)} />

      {/* ---- console ---- */}
      <main className={`console${controlOpen ? '' : ' collapsed'}`}>
        <button
          className="panel-toggle"
          onClick={() => setControlOpen((v) => !v)}
          aria-expanded={controlOpen}
          aria-label={controlOpen ? 'Hide control panel' : 'Show control panel'}
          title={controlOpen ? 'Hide control panel' : 'Show control panel'}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <line x1="15" y1="4" x2="15" y2="20" />
          </svg>
        </button>

        <section className={`grid${spotlight ? ' spotlight' : ''}`}>
          {loading && <div className="feed-loading">connecting to tower…</div>}
          {cameras.map((c) => (
            <TowerFeed
              key={c.id}
              camera={c}
              selected={c.id === selectedCam?.id}
              accent={ACCENT}
              spotlighted={spotlight && c.id === selectedCam?.id}
              thumb={spotlight && c.id !== selectedCam?.id}
              onSelect={() => setSelectedCamId(c.id)}
              onToggleSpotlight={() => setSpotlight((v) => !v)}
              onSnapshot={() => captureSnapshot(c.id)}
            />
          ))}
        </section>

        {selectedCam && (
          <aside className="control">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', paddingBottom: 12, paddingRight: 34, borderBottom: `1px solid ${colors.line}` }}>
              <div>
                <div style={{ fontFamily: font.mono, fontSize: 10, letterSpacing: '.24em', color: colors.textFaint }}>CAMERA UNDER CONTROL</div>
                <div style={{ fontFamily: font.display, fontWeight: 700, fontSize: 20, letterSpacing: '.1em', color: ACCENT, marginTop: 3 }}>{selectedCam.label}</div>
              </div>
              <div style={{ fontFamily: font.mono, fontSize: 11, color: colors.text, letterSpacing: '.1em' }}>CAM {selectedCam.id}</div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <Metric k="AZIMUTH" v={`${pad3(selectedCam.az)}°`} />
              <Metric k="ELEVATION" v={`${elFmt(selectedCam.el)}°`} />
              <Metric k="ZOOM" v={`${selectedCam.zoom.toFixed(1)}×`} />
              <Metric k="STREAM" v={selectedCam.status === 'ONLINE' ? 'LIVE' : selectedCam.status === 'OFFLINE' ? 'DOWN' : '—'} />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <PtzSpeedSlider value={ptzSpeedPct} onChange={setSpeed} accent={ACCENT} />
              <div style={{ fontFamily: font.mono, fontSize: 10, letterSpacing: '.24em', color: colors.textFaint }}>PAN / TILT</div>
              <div style={{ display: 'flex', justifyContent: 'center' }}>
                <PtzPad accent={ACCENT} onPanStart={panStart} onPanEnd={panEnd} onRecenter={recenter} />
              </div>
            </div>

            <div style={{ display: 'flex', gap: 8 }}>
              <button className="ctl-btn" onClick={() => zoomBy(-ZOOM_STEP)}>− ZOOM</button>
              <div style={{ flex: '0 0 78px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', border: `1px solid ${colors.line}`, borderRadius: 6, background: colors.bgWell }}>
                <span style={{ fontFamily: font.mono, fontSize: 16, color: colors.textBright }}>{selectedCam.zoom.toFixed(1)}×</span>
                <span style={{ fontFamily: font.mono, fontSize: 9, letterSpacing: '.14em', color: colors.textFaint }}>OPTICAL</span>
              </div>
              <button className="ctl-btn" onClick={() => zoomBy(ZOOM_STEP)}>ZOOM +</button>
            </div>

            <button className="rec-btn" disabled title="Recording is configured on the device (device.env: RECORD_ENABLE)">
              <span style={{ width: 9, height: 9, borderRadius: '50%', background: colors.textFaint }} />
              RECORDING · DEVICE-MANAGED
            </button>

            <div style={{ minHeight: 14, fontFamily: font.mono, fontSize: 10, letterSpacing: '.08em', color: ptzMsg.includes('failed') ? colors.offline : colors.textFaint }}>
              {ptzMsg}
            </div>

            <div style={{ marginTop: 'auto', paddingTop: 12, borderTop: `1px solid ${colors.line}`, fontFamily: font.mono, fontSize: 10, lineHeight: 1.8, color: colors.textFaint }}>
              <Key>1–{Math.max(1, cameras.length)}</Key> select · <Key>↑↓←→</Key> nudge · hold pad to jog · <Key>+ −</Key> zoom · <Key>space</Key> home
            </div>
          </aside>
        )}
      </main>

      <SensorPanel open={panelOpen} deviceName={deviceName} sensors={sensors} alerts={alerts} connected={connected} onClose={() => setPanelOpen(false)} />
    </div>
  );
}

function Metric({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ background: colors.bgWell, border: `1px solid ${colors.line}`, borderRadius: 6, padding: '8px 10px' }}>
      <div style={{ fontFamily: font.mono, fontSize: 9, letterSpacing: '.18em', color: colors.textFaint }}>{k}</div>
      <div style={{ fontFamily: font.mono, fontSize: 17, color: colors.textBright, marginTop: 2 }}>{v}</div>
    </div>
  );
}

function Key({ children }: { children: ReactNode }) {
  return <b style={{ color: colors.text, fontWeight: 500 }}>{children}</b>;
}
