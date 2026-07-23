import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { Camera, CameraStatus } from '../types';
import { colors, font } from '../tokens';
import { formatClockUTC1 } from '../clock';
import { health, levelColor } from '../util';
import { buildSensors } from '../sensors';
import { ptz, snapshot, getRecording, setRecording } from '../api';
import type { RecordingStatus } from '../api';
import { useTower } from '../useTower';
import { usePtzPositions } from '../usePtzPositions';
import { formatZoom } from '../ptzMetrics';
import TowerFeed from './TowerFeed';
import PtzPad, { type PanDir } from './PtzPad';
import PtzSpeedSlider, { speedToVelocity } from './PtzSpeedSlider';
import SensorBar from './SensorBar';
import SensorPanel from './SensorPanel';

const ACCENT = colors.accent;
const ZOOM_STEP = 0.3;
const PTZ_PULSE_SEC = 0.2;   // fixed tap/hold pulse — distance scales with speed slider
const PTZ_HOLD_MS = 180;     // press longer than this → jog instead of single nudge
const PTZ_JOG_MS = 280;      // repeat interval while holding pad
const PTZ_SPEED_KEY = 'sentinel-ptz-speed';

const pad3 = (n: number) => String(((Math.round(n) % 360) + 360) % 360).padStart(3, '0');
const elFmt = (e: number) => (e >= 0 ? '+' : '-') + String(Math.abs(Math.round(e))).padStart(2, '0');

const isFormField = (el: EventTarget | null) => {
  const t = el as HTMLElement | null;
  return !!t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
};

interface Props {
  onOpenRecordings?: () => void;
}

export default function SentinelConsole({ onOpenRecordings }: Props) {
  const { config, streams, status, alerts, connected } = useTower();

  const [selectedCamId, setSelectedCamId] = useState<string>('');
  const [now, setNow] = useState(() => Date.now());
  const [controlOpen, setControlOpen] = useState(true);
  const [spotlight, setSpotlight] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [ptzMsg, setPtzMsg] = useState('');
  const [recording, setRecordingState] = useState<RecordingStatus | null>(null);
  const [recBusy, setRecBusy] = useState(false);
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

  const cameraNums = useMemo(
    () => (config?.cameras ?? []).map((c) => c.camera),
    [config],
  );

  const { positions, refreshSoon } = usePtzPositions(cameraNums, readyByPath, selectedCamId);

  const cameras: Camera[] = useMemo(() => {
    const gw = config?.cameras ?? [];
    return gw.map((c) => {
      const id = String(c.camera).padStart(2, '0');
      const pos = positions[id];
      let cstatus: CameraStatus = 'STANDBY';
      if (streams && streams.available) cstatus = readyByPath.get(c.path) ? 'ONLINE' : 'OFFLINE';
      return {
        id,
        path: c.path,
        label: (c.label || c.path).toUpperCase(),
        status: cstatus,
        mjpegUrl: c.mjpeg_url ?? undefined,
        hlsUrl: c.hls_url || undefined,
        az: pos?.az ?? 0,
        el: pos?.el ?? 0,
        zoom: pos?.zoom ?? 0,
        ptzLive: !!pos,
        recording: !!(recording?.enabled),
        recStart: null,
        homeAz: 0, homeEl: 0,
      };
    });
  }, [config, streams, readyByPath, positions, recording]);

  const deviceName = (config?.device_id || 'SENTINEL TOWER').toUpperCase();
  const selectedCam = cameras.find((c) => c.id === selectedCamId) ?? cameras[0];
  const sensors = useMemo(() => buildSensors(status, streams, cameras), [status, streams, cameras]);
  const sysHealth = health(sensors);

  useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      try {
        const s = await getRecording();
        if (!cancelled) setRecordingState(s);
      } catch {
        /* gateway down — leave last known */
      }
    };
    void pull();
    const id = window.setInterval(pull, 5000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  // keep a valid selection as cameras (re)appear
  useEffect(() => {
    if (cameras.length && !cameras.some((c) => c.id === selectedCamId)) {
      setSelectedCamId(cameras[0].id);
    }
  }, [cameras, selectedCamId]);

  const camNum = useCallback((id: string) => parseInt(id, 10) || 1, []);

  const feedback = useCallback((res: { ok?: boolean; error?: { message?: string } }, verb: string) => {
    if (res && res.ok === false) setPtzMsg(`${verb} failed: ${res.error?.message ?? 'error'}`);
    else setPtzMsg(`${verb} ok`);
  }, []);

  const toggleRecording = useCallback(async () => {
    if (recBusy) return;
    const next = !(recording?.enabled);
    setRecBusy(true);
    setPtzMsg(next ? 'enabling recording…' : 'stopping recording…');
    try {
      const res = await setRecording(next);
      setRecordingState(res);
      if (res.error) {
        setPtzMsg(`recording failed: ${res.error.message ?? res.error.code ?? 'error'}`);
      } else if (res.persist_ok === false) {
        setPtzMsg(`recording ${next ? 'ON' : 'OFF'} (live) — persist warning`);
      } else {
        setPtzMsg(`recording ${next ? 'ON' : 'OFF'}`);
      }
      if (res.warnings?.length) {
        console.warn('recording warnings', res.warnings);
      }
    } catch (e) {
      setPtzMsg(`recording failed: ${String(e)}`);
    } finally {
      setRecBusy(false);
    }
  }, [recBusy, recording?.enabled]);

  const setSpeed = useCallback((pct: number) => {
    setPtzSpeedPct(pct);
    try { localStorage.setItem(PTZ_SPEED_KEY, String(pct)); } catch { /* noop */ }
  }, []);

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
    }).then((r) => { refreshSoon(); return r; });
  }, [selectedCam, camNum, ptzVelocity, refreshSoon]);

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
      void ptz('stop', { camera: cam }).then(() => refreshSoon());
    }
    jogDir.current = null;
  }, [selectedCam, camNum, refreshSoon]);

  const nudge = useCallback((dir: PanDir) => {
    void sendMove(dir, 0, PTZ_PULSE_SEC).then((r) => feedback(r, 'move'));
  }, [sendMove, feedback]);

  const panStart = useCallback((dir: PanDir) => {
    stopJog();
    setPtzMsg(`ptz ${dir}…`);
    jogDir.current = dir;
    void sendMove(dir, 0, PTZ_PULSE_SEC).then((r) => feedback(r, 'move'));
    jogTimer.current = window.setTimeout(() => {
      jogTimer.current = undefined;
      jogging.current = true;
      setPtzMsg('jog…');
      jogInterval.current = window.setInterval(() => {
        void sendMove(dir, 0, PTZ_PULSE_SEC);
      }, PTZ_JOG_MS);
    }, PTZ_HOLD_MS);
  }, [stopJog, sendMove, feedback]);

  const panEnd = useCallback(() => {
    if (jogTimer.current !== undefined) {
      window.clearTimeout(jogTimer.current);
      jogTimer.current = undefined;
      jogDir.current = null;
      return;
    }
    stopJog();
  }, [stopJog]);

  const pan = useCallback((dir: PanDir) => {
    nudge(dir);
  }, [nudge]);

  const zoomBy = useCallback((d: number) => {
    const dir = d > 0 ? 1 : -1;
    void sendMove(null, dir, PTZ_PULSE_SEC).then((r) => feedback(r, 'zoom'));
  }, [sendMove, feedback]);

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
    ptz('home', { camera: cam }).then((r) => { feedback(r, 'home'); refreshSoon(); });
  }, [selectedCam, camNum, feedback, refreshSoon]);

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

  const utc = formatClockUTC1(now);
  const loading = cameras.length === 0;

  return (
    <div className="app">
      {/* ---- top bar ---- */}
      <header className="topbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontFamily: font.display, fontWeight: 700, fontSize: 24, letterSpacing: '.24em', color: colors.textBright }}>SENTINEL</span>
          <span style={{ fontFamily: font.display, fontWeight: 500, fontSize: 14, letterSpacing: '.32em', color: colors.textFaint }}>TOWER CONTROL</span>
          <nav className="tower-nav" aria-label="Main sections">
            <button type="button" className="active">LIVE</button>
            {onOpenRecordings && (
              <button type="button" onClick={onOpenRecordings}>RECORDINGS</button>
            )}
          </nav>
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
          <div style={{ fontFamily: font.mono, fontSize: 10, letterSpacing: '.16em', color: colors.textFaint }}>UTC+1 · {connected ? 'LINK OK' : 'NO LINK'}</div>
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
              <Metric k="AZIMUTH" v={selectedCam.ptzLive ? `${pad3(selectedCam.az)}°` : '—'} />
              <Metric k="ELEVATION" v={selectedCam.ptzLive ? `${elFmt(selectedCam.el)}°` : '—'} />
              <Metric k="ZOOM" v={selectedCam.ptzLive ? formatZoom(selectedCam.zoom) : '—'} />
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
              <button className="ctl-btn" onClick={() => zoomBy(-ZOOM_STEP)} title="Zoom out">− ZOOM</button>
              <div style={{ flex: '0 0 78px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', border: `1px solid ${colors.line}`, borderRadius: 6, background: colors.bgWell }}>
                <span style={{ fontFamily: font.mono, fontSize: 16, color: colors.textBright }}>
                  {selectedCam.ptzLive ? formatZoom(selectedCam.zoom) : '—'}
                </span>
                <span style={{ fontFamily: font.mono, fontSize: 9, letterSpacing: '.14em', color: colors.textFaint }}>ZOOM</span>
              </div>
              <button className="ctl-btn" onClick={() => zoomBy(ZOOM_STEP)} title="Zoom in">ZOOM +</button>
            </div>

            <button
              className={`rec-btn${recording?.enabled ? ' rec-btn--on' : ''}`}
              disabled={recBusy || !connected}
              title={recording?.enabled ? 'Stop continuous recording' : 'Start continuous recording'}
              onClick={() => void toggleRecording()}
            >
              <span
                className={recording?.enabled ? 'rec-dot' : undefined}
                style={{
                  width: 9,
                  height: 9,
                  borderRadius: '50%',
                  background: recording?.enabled ? colors.offline : colors.textFaint,
                }}
              />
              {recBusy
                ? 'RECORDING…'
                : recording?.enabled
                  ? 'RECORDING · ON'
                  : 'RECORDING · OFF'}
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
