import { useCallback, useEffect, useMemo, useState } from 'react';
import { colors, font } from '../tokens';
import { formatClockUTC1, formatDateTimeUTC1 } from '../clock';
import { getLocalRecordings, type LocalRecordingSegment } from '../api';

const PAGE_SIZE = 24;

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
  const [page, setPage] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getLocalRecordings({ camera, limit: 500 });
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

  useEffect(() => {
    setPage(0);
  }, [camera]);

  const pageCount = Math.max(1, Math.ceil(segments.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageItems = useMemo(
    () => segments.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE),
    [segments, safePage],
  );
  const rangeStart = segments.length === 0 ? 0 : safePage * PAGE_SIZE + 1;
  const rangeEnd = Math.min(segments.length, (safePage + 1) * PAGE_SIZE);

  return (
    <div className="recordings-view">
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
        <aside className="rec-list-pane">
          <div className="rec-list-head">
            <span>LOCAL SEGMENTS</span>
            <span>{segments.length} total</span>
          </div>
          <div className="rec-list-body">
            {segments.length === 0 && !loading && (
              <div className="rec-empty">No local recordings found.</div>
            )}
            {pageItems.map((s) => (
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
          <div className="rec-pager">
            <button
              type="button"
              className="ctl-btn"
              disabled={safePage <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              PREV
            </button>
            <span className="rec-pager-meta">
              {rangeStart}–{rangeEnd} / {segments.length} · page {safePage + 1}/{pageCount}
            </span>
            <button
              type="button"
              className="ctl-btn"
              disabled={safePage >= pageCount - 1}
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
            >
              NEXT
            </button>
          </div>
        </aside>

        <section className="rec-player-pane">
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
            <div className="rec-player-empty">Local MP4 playback from this tower’s disk.</div>
          )}
        </section>
      </div>
    </div>
  );
}
