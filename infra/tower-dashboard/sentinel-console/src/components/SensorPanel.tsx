import type { AlertEvent, Sensor } from '../types';
import { alertColor, levelColor } from '../util';

interface Props {
  open: boolean;
  deviceName: string;
  sensors: Sensor[];
  alerts: AlertEvent[];
  connected: boolean;
  onClose: () => void;
}

const badgeText = (s: Sensor) => (s.available === false ? 'N/A' : s.level === 'crit' ? 'ALERT' : s.level === 'warn' ? 'WARN' : 'OK');
const clampPct = (n: number) => Math.max(0, Math.min(100, n));

function Card({ s }: { s: Sensor }) {
  const naCard = s.available === false;
  const c = naCard ? '#4a5762' : levelColor(s.level);
  return (
    <div className={`mcard ${naCard ? 'na' : s.level}`}>
      <div className="mc-top">
        <span className="mc-k">{s.label}</span>
        <span className="mc-badge" style={{ color: c, borderColor: c }}>{badgeText(s)}</span>
      </div>

      {naCard ? (
        <div className="mc-v mc-na">—</div>
      ) : (
        <>
          {s.kind === 'numeric' && (
            <>
              <div className="mc-v">{s.value}{s.unit && <u>{s.unit}</u>}</div>
              {s.max != null && (
                <div className="mc-bar">
                  <span style={{
                    width: `${clampPct((s.value / s.max) * 100)}%`,
                    background: s.barGradient ? `linear-gradient(90deg, ${levelColor('ok')}, ${levelColor('warn')})` : c,
                  }} />
                </div>
              )}
            </>
          )}
          {s.kind === 'state' && <div className="mc-v">{s.state}</div>}
          {s.kind === 'list' && (
            <div className="mc-list">
              {s.items.map((it) => (
                <div className="lr" key={it.label}>
                  <span>{it.label}</span>
                  <span className={`s ${it.up ? 'live' : 'down'}`}><span className="d" />{it.up ? 'live' : 'down'}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {s.detail && !naCard && <div className="mc-sub">{s.detail}</div>}
    </div>
  );
}

function AlertRow({ a }: { a: AlertEvent }) {
  const hasPayload = a.payload && Object.keys(a.payload).length > 0;
  return (
    <div className="ai" style={{ borderLeftColor: alertColor(a.level) }}>
      <div className="ai-top">
        <span className="ai-type" style={{ color: alertColor(a.level) }}>{a.type}</span>
        <span className="ai-time">{a.time}</span>
      </div>
      {hasPayload && <div className="ai-json">{JSON.stringify(a.payload)}</div>}
    </div>
  );
}

export default function SensorPanel({ open, deviceName, sensors, alerts, connected, onClose }: Props) {
  return (
    <div className={`mon-root${open ? ' open' : ''}`} aria-hidden={!open}>
      <div className="mon-backdrop" onClick={onClose} />
      <div className="mon" role="dialog" aria-modal="true" aria-label={`${deviceName} sensor monitor`}>
        <div className="mon-top">
          <div className="mon-brand">{deviceName}<small>SENSOR MONITOR</small></div>
          <span className="mon-conn">
            <span className="d" style={{ background: connected ? '#ffffff' : '#FF5A5A', boxShadow: `0 0 8px ${connected ? '#ffffff' : '#FF5A5A'}` }} />
            {connected ? 'CONNECTED' : 'DISCONNECTED'}
          </span>
          <button className="mon-close" onClick={onClose} aria-label="Close monitor" title="Close">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden="true">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="mon-body">
          <div className="cards">
            {sensors.map((s) => <Card key={s.key} s={s} />)}
          </div>

          <div className="alerts">
            <div className="al-head"><span className="t">LIVE ALERTS</span><span className="al-count">{alerts.length}</span></div>
            <div className="al-list">
              {alerts.length === 0 && <div className="al-empty">No active alerts.</div>}
              {alerts.map((a) => <AlertRow key={a.id} a={a} />)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
