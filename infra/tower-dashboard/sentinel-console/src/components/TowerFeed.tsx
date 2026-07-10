import { useState } from 'react';
import type { CSSProperties } from 'react';
import type { Camera } from '../types';
import { colors, font } from '../tokens';
import { formatZoom } from '../ptzMetrics';
import CameraVideo from './CameraVideo';

interface Props {
  camera: Camera;
  selected: boolean;
  accent: string;
  spotlighted: boolean;
  thumb: boolean;
  onSelect: () => void;
  onToggleSpotlight: () => void;
  onSnapshot: () => Promise<string | null>;
}

const pad3 = (n: number) => String(((Math.round(n) % 360) + 360) % 360).padStart(3, '0');
const elFmt = (e: number) => (e >= 0 ? '+' : '-') + String(Math.abs(Math.round(e))).padStart(2, '0');

/**
 * One camera tile: live video (MJPEG/HLS) under a targeting HUD. The bottom
 * toolbar (only on the selected tile) drives spotlight / snapshot; PTZ + zoom
 * live in the control panel. Recording is managed by the device (device.env),
 * so this tile only reflects the recording flag — it does not toggle it.
 */
export default function TowerFeed({
  camera, selected, accent, spotlighted, thumb, onSelect, onToggleSpotlight, onSnapshot,
}: Props) {
  const [flash, setFlash] = useState(false);
  const [saveNote, setSaveNote] = useState<string | null>(null);
  const hasSource = !!(camera.mjpegUrl || camera.hlsUrl);
  const streamReady = camera.status === 'ONLINE';

  const statusColor =
    camera.status === 'ONLINE' ? accent : camera.status === 'STANDBY' ? colors.standby : colors.offline;
  const bracket = selected ? accent : colors.bracketIdle;
  const bracketBase: CSSProperties = { position: 'absolute', width: 22, height: 22, pointerEvents: 'none' };

  const snapshot = () => {
    setFlash(true);
    window.setTimeout(() => setFlash(false), 380);
    onSnapshot().then((path) => {
      if (path) {
        setSaveNote(`Saved to ${path}`);
        window.setTimeout(() => setSaveNote(null), 4500);
      }
    });
  };

  const feedClass = `feed${selected ? ' selected' : ''}${spotlighted ? ' is-spotlight' : ''}${thumb ? ' is-thumb' : ''}`;

  return (
    <div
      className={feedClass}
      onClick={onSelect}
      style={{
        position: 'relative', width: '100%', height: '100%', minHeight: 0, overflow: 'hidden',
        cursor: 'pointer', userSelect: 'none',
        background: `linear-gradient(180deg, ${colors.feedTop} 0%, ${colors.feedMid} 46%, ${colors.feedBot} 100%)`,
      }}
    >
      {/* texture (only when there is no live source to show) */}
      {!hasSource && (
        <>
          <div style={{ position: 'absolute', inset: 0, opacity: 0.3, pointerEvents: 'none', background: 'repeating-linear-gradient(115deg,#0d1216 0,#0d1216 10px,#171e24 10px,#171e24 20px)' }} />
          <div style={{ position: 'absolute', inset: 0, opacity: 0.5, pointerEvents: 'none', background: 'repeating-linear-gradient(0deg,rgba(0,0,0,0) 0,rgba(0,0,0,0) 2px,rgba(0,0,0,.16) 3px)' }} />
        </>
      )}

      {/* live video */}
      {hasSource && <CameraVideo mjpegUrl={camera.mjpegUrl} hlsUrl={camera.hlsUrl} streamReady={streamReady} />}

      {/* corner brackets */}
      <div style={{ ...bracketBase, top: 10, left: 10, borderTop: `2px solid ${bracket}`, borderLeft: `2px solid ${bracket}` }} />
      <div style={{ ...bracketBase, top: 10, right: 10, borderTop: `2px solid ${bracket}`, borderRight: `2px solid ${bracket}` }} />
      <div style={{ ...bracketBase, bottom: 10, left: 10, borderBottom: `2px solid ${bracket}`, borderLeft: `2px solid ${bracket}` }} />
      <div style={{ ...bracketBase, bottom: 10, right: 10, borderBottom: `2px solid ${bracket}`, borderRight: `2px solid ${bracket}` }} />

      {/* crosshair (only over the placeholder) */}
      {!hasSource && (
        <>
          <div style={{ position: 'absolute', top: '50%', left: '50%', width: 1, height: 56, transform: 'translate(-50%,-50%)', background: 'rgba(255,255,255,.14)', pointerEvents: 'none' }} />
          <div style={{ position: 'absolute', top: '50%', left: '50%', width: 56, height: 1, transform: 'translate(-50%,-50%)', background: 'rgba(255,255,255,.14)', pointerEvents: 'none' }} />
          <div style={{ position: 'absolute', top: '50%', left: '50%', width: 26, height: 26, transform: 'translate(-50%,-50%)', border: '1px solid rgba(255,255,255,.18)', borderRadius: '50%', pointerEvents: 'none' }} />
        </>
      )}

      {/* top strip: id / label / status / rec */}
      <div style={{ position: 'absolute', top: 0, left: 0, right: 0, display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', padding: '14px 16px', background: 'linear-gradient(180deg,rgba(6,9,11,.72),transparent)', pointerEvents: 'none' }}>
        <div>
          <div style={{ fontFamily: font.display, fontWeight: 700, fontSize: 19, letterSpacing: '.06em', color: colors.textBright, lineHeight: 1 }}>
            CAM {camera.id}
          </div>
          <div style={{ fontFamily: font.mono, fontSize: 10, letterSpacing: '.14em', color: colors.textDim, marginTop: 4 }}>
            {camera.label}
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 8 }}>
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontFamily: font.mono, fontSize: 10, letterSpacing: '.1em', color: statusColor }}>
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: statusColor, boxShadow: `0 0 8px ${statusColor}` }} />
            {camera.status}
          </div>
          {camera.recording && (
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '3px 8px', background: 'rgba(255,59,59,.14)', border: '1px solid rgba(255,59,59,.55)', borderRadius: 4, fontFamily: font.mono, fontSize: 10, fontWeight: 700, letterSpacing: '.12em', color: colors.recText }}>
              <span className="rec-dot" style={{ width: 7, height: 7, borderRadius: '50%', background: colors.rec }} />
              REC
            </div>
          )}
        </div>
      </div>

      {/* bottom strip: live PTZ / zoom from camera */}
      <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', background: 'linear-gradient(0deg,rgba(6,9,11,.72),transparent)', fontFamily: font.mono, fontSize: 10, letterSpacing: '.1em', color: colors.textFaint, pointerEvents: 'none' }}>
        <span>
          PTZ {pad3(camera.az)}·{elFmt(camera.el)}
          &nbsp; {camera.ptzLive ? `Z${formatZoom(camera.zoom)}` : 'Z—'}
        </span>
        <span>RTSP · LIVE</span>
      </div>

      {flash && <div className="snap-flash" style={{ position: 'absolute', inset: 0, background: '#eef3f7', pointerEvents: 'none' }} />}
      {saveNote && <div className="snap-toast">{saveNote}</div>}

      {selected && <div style={{ position: 'absolute', inset: 0, border: `1.5px solid ${accent}`, pointerEvents: 'none' }} />}

      {/* action toolbar — only on the selected tile */}
      {selected && (
        <div className="tile-toolbar" onClick={(e) => e.stopPropagation()}>
          <button
            className="tile-btn"
            onClick={onToggleSpotlight}
            aria-pressed={spotlighted}
            aria-label={spotlighted ? 'Exit fullscreen' : 'Fullscreen this feed'}
            title={spotlighted ? 'Exit fullscreen' : 'Fullscreen'}
          >
            {spotlighted ? (
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M8 3v3a2 2 0 0 1-2 2H3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M16 21v-3a2 2 0 0 1 2-2h3" />
              </svg>
            ) : (
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3" />
              </svg>
            )}
          </button>

          <button className="tile-btn" onClick={snapshot} aria-label="Capture snapshot" title="Snapshot">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
              <circle cx="12" cy="13" r="4" />
            </svg>
          </button>
        </div>
      )}
    </div>
  );
}
