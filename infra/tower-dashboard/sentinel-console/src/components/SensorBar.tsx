import type { Sensor } from '../types';
import { colors } from '../tokens';
import { levelColor } from '../util';

interface Props {
  sensors: Sensor[];
  deviceName: string;
  connected: boolean;
  onOpenDetail: () => void;
}

/**
 * Tower context bar — this tower's live vitals as a row of status chips, plus a
 * DETAILS button that opens the full sensor panel. Only sensors the tower
 * actually reports are shown as chips.
 */
export default function SensorBar({ sensors, deviceName, connected, onOpenDetail }: Props) {
  const chips = sensors.filter((s) => s.inBar && s.available !== false);

  return (
    <div className="sensorbar">
      <div className="sb-label">
        <span className="k">TOWER SENSORS</span>
        <span className="v">{deviceName}</span>
      </div>

      {chips.map((s) => {
        const c = levelColor(s.level);
        return (
          <div key={s.key} className="chip">
            <span className="cdot" style={{ background: c, color: c }} />
            <div className="meta">
              <span className="ck">{s.short}</span>
              <span className="cv" style={{ color: s.level === 'ok' ? colors.textBright : c }}>
                {s.kind === 'numeric' ? (
                  <>{s.value}{s.unit && <u>{s.unit}</u>}</>
                ) : s.kind === 'state' ? (
                  s.state
                ) : null}
              </span>
            </div>
          </div>
        );
      })}

      <div className="sb-right">
        <span className="sb-live">
          <span className="sb-live-dot" style={{ background: connected ? colors.accent : colors.offline, boxShadow: `0 0 8px ${connected ? colors.accent : colors.offline}` }} />
          {connected ? 'LIVE' : 'OFFLINE'}
        </span>
        <button className="sb-more" onClick={onOpenDetail} aria-label="Open sensor detail panel" title="Sensor details">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <line x1="14" y1="4" x2="14" y2="20" />
          </svg>
          DETAILS
        </button>
      </div>
    </div>
  );
}
