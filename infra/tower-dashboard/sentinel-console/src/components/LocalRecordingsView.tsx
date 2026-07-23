import { useCallback, useEffect, useState } from 'react';
import { colors, font } from '../tokens';
import { formatClockUTC1, formatDateTimeUTC1 } from '../clock';
import { getLocalRecordings, type LocalRecordingSegment } from '../api';

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

interface Props {
  onBackToLive?: () => void;
}

/** Browse / play local MP4 segments from RECORD_PATH on this tower (not cloud). */
export default function LocalRecordingsView({ onBackToLive }: Props) {
  const [camera, setCamera] = useState<number | undefined>(undefined);
  const [segments, setSegments] = useState<LocalRecordingSegment[]>([]);
  const [meta, setMeta] = useState<{ record_path?: string; segment_duration?: string; delete_after_effective?: string }>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [playing, setPlaying] = useState<LocalRecordingSegment | null>(null);
  const [now] = useState(() => Date.now());

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getLocalRecordings({ camera, limit: 300 });
      setSegments(data.segments ?? []);
      setMeta({
        record_path: data.record_path,
        segment_duration: data.segment_duration,
        delete_after_effective: data.delete_after_effective,
      });
      if (data.error) setError(data.error);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSegments([]);
    } finally {
      setLoading(false);
    }
  }, [camera]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="app recordings-view">
      <header className="topbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontFamily: font.display, fontWeight: 700, fontSize: 24, letterSpacing: '.24em', color: colors.textBright }}>SENTINEL</span>
          <span style={{ fontFamily: font.display, fontWeight: 500, fontSize: 14, letterSpacing: '.32em', color: colors.textFaint }}>RECORDINGS</span>
          {onBackToLive && (
            <button type="button" className="ctl-btn" style={{ flex: 'none', padding: '8px 12px' }} onClick={onBackToLive}>
              ← LIVE
            </button>
          )}
        </div>
        <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
          <div style={{ fontFamily: font.mono, fontSize: 19, color: colors.textBright, letterSpacing: '.06em' }}>{formatClockUTC1(now)}</div>
          <div style={{ fontFamily: font.mono, fontSize: 10, letterSpacing: '.16em', color: colors.textFaint }}>
            LOCAL DISK · {meta.segment_duration ?? '—'} SEG · DELETE {meta.delete_after_effective ?? '—'}
          </div>
        </div>
      </header>

      <div className="rec-toolbar">
        <label className="rec-filter">
          <span>CAMERA</span>
          <select
            value={camera ?? ''}
            onChange={(e) => setCamera(e.target.value ? Number(e.target.value) : undefined)}
          >
            <option value="">All</option>
            {[1, 2, 3, 4].map((n) => (
              <option key={n} value={n}>CAM {String(n).padStart(2, '0')}</option>
            ))}
          </select>
        </label>
        <button type="button" className="ctl-btn" style={{ flex: 'none' }} onClick={() => void refresh()} disabled={loading}>
          {loading ? 'LOADING…' : 'REFRESH'}
        </button>
        {meta.record_path && (
          <span style={{ fontFamily: font.mono, fontSize: 10, color: colors.textFaint, letterSpacing: '.06em' }}>
            {meta.record_path}
          </span>
        )}
      </div>

      {error && <div className="login-error rec-error">{error}</div>}

      <div className="rec-layout">
        <div className="rec-list">
          <div className="rec-list-head">
            <span>LOCAL SEGMENTS</span>
            <span>{segments.length}</span>
          </div>
          {segments.length === 0 && !loading && (
            <div className="rec-empty">No local recordings found.</div>
          )}
          {segments.map((s) => (
            <div key={s.rel_path} className="rec-row">
              <div className="rec-row-title">CAM {String(s.camera).padStart(2, '0')} · {s.filename}</div>
              <div className="rec-row-time">{formatDateTimeUTC1(s.mtime_utc)}</div>
              <div className="rec-row-meta">{formatBytes(s.size_bytes)}</div>
              <div className="rec-row-actions">
                <button type="button" className="ctl-btn" onClick={() => setPlaying(s)}>Play</button>
              </div>
            </div>
          ))}
        </div>
        <div className="rec-player-pane">
          <div className="rec-player-head">
            {playing ? `CAM ${String(playing.camera).padStart(2, '0')} · ${playing.filename}` : 'Select a segment to play'}
          </div>
          {playing ? (
            <video
              key={playing.playback_url}
              className="rec-player"
              controls
              autoPlay
              playsInline
              preload="metadata"
              src={playing.playback_url}
            />
          ) : (
            <div className="rec-empty">Local MP4 playback from this tower’s disk.</div>
          )}
        </div>
      </div>
    </div>
  );
}
