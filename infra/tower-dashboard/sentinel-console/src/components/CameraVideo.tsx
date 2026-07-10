import { useEffect, useRef, useState } from 'react';
import Hls from 'hls.js';

interface Props {
  mjpegUrl?: string;
  hlsUrl?: string;
  /** mediamtx path readiness — when false, don't spin ffmpeg retries */
  streamReady?: boolean;
}

type Mode = 'mjpeg' | 'hls' | 'none';

const MJPEG_MAX_ERRORS = 3;
const STALL_MS = 18_000;
const STALL_CHECK_MS = 4_000;

/**
 * Live camera video for one tile. Prefers the low-latency MJPEG proxy; if it
 * cannot connect (or is not configured) it falls back to HLS via hls.js —
 * mirroring the behaviour of the previous vanilla dashboard.
 */
export default function CameraVideo({ mjpegUrl, hlsUrl, streamReady = true }: Props) {
  const initialMode: Mode = mjpegUrl ? 'mjpeg' : hlsUrl ? 'hls' : 'none';
  const [mode, setMode] = useState<Mode>(initialMode);
  const [note, setNote] = useState<string>('connecting…');
  const [playing, setPlaying] = useState(false);

  const imgRef = useRef<HTMLImageElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const mjpegErrors = useRef(0);
  const retryTimer = useRef<number | undefined>(undefined);
  const lastFrameAt = useRef(0);

  // Reset when the source changes (e.g. config (re)loaded).
  useEffect(() => {
    mjpegErrors.current = 0;
    lastFrameAt.current = 0;
    setPlaying(false);
    setNote('connecting…');
    setMode(mjpegUrl ? 'mjpeg' : hlsUrl ? 'hls' : 'none');
  }, [mjpegUrl, hlsUrl]);

  // MJPEG stall watchdog: frozen <img> streams often never fire error.
  useEffect(() => {
    if (mode !== 'mjpeg' || !mjpegUrl || !streamReady) return;
    const id = window.setInterval(() => {
      const img = imgRef.current;
      if (!img || !playing) return;
      const age = Date.now() - lastFrameAt.current;
      if (lastFrameAt.current > 0 && age > STALL_MS) {
        setNote('recovering stream…');
        img.src = `${mjpegUrl}?t=${Date.now()}`;
        lastFrameAt.current = Date.now();
      }
    }, STALL_CHECK_MS);
    return () => window.clearInterval(id);
  }, [mode, mjpegUrl, streamReady, playing]);

  // MJPEG: <img> streaming multipart; retry with cache-bust, fall back to HLS.
  useEffect(() => {
    if (mode !== 'mjpeg' || !mjpegUrl) return;
    if (!streamReady) {
      setPlaying(false);
      setNote('camera offline');
      if (imgRef.current) imgRef.current.removeAttribute('src');
      return;
    }
    const img = imgRef.current;
    if (!img) return;

    const onLoad = () => { mjpegErrors.current = 0; lastFrameAt.current = Date.now(); setPlaying(true); setNote(''); };
    const onError = () => {
      setPlaying(false);
      mjpegErrors.current += 1;
      if (mjpegErrors.current >= MJPEG_MAX_ERRORS && hlsUrl) {
        setNote('switching to HLS…');
        setMode('hls');
        return;
      }
      setNote('stream unavailable — retrying…');
      retryTimer.current = window.setTimeout(() => {
        if (imgRef.current) imgRef.current.src = `${mjpegUrl}?t=${Date.now()}`;
      }, 3000);
    };

    img.addEventListener('load', onLoad);
    img.addEventListener('error', onError);
    img.src = mjpegUrl;
    return () => {
      img.removeEventListener('load', onLoad);
      img.removeEventListener('error', onError);
      if (retryTimer.current) window.clearTimeout(retryTimer.current);
    };
  }, [mode, mjpegUrl, hlsUrl, streamReady]);

  // HLS: hls.js where supported, native HLS on Safari/iOS.
  useEffect(() => {
    if (mode !== 'hls' || !hlsUrl) return;
    const video = videoRef.current;
    if (!video) return;

    const onPlaying = () => { setPlaying(true); setNote(''); };
    const onWaiting = () => setNote('buffering…');
    video.addEventListener('playing', onPlaying);
    video.addEventListener('waiting', onWaiting);

    let hls: Hls | undefined;
    let reattach: number | undefined;

    if (Hls.isSupported()) {
      hls = new Hls({ liveSyncDurationCount: 1, backBufferLength: 4, lowLatencyMode: true, manifestLoadingRetryDelay: 1500 });
      hls.on(Hls.Events.MANIFEST_PARSED, () => { video.play().catch(() => setNote('tap to play')); });
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (!data || !data.fatal) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          setNote('stream reconnecting…');
          window.setTimeout(() => { try { hls?.startLoad(); } catch { /* gone */ } }, 1500);
        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          try { hls?.recoverMediaError(); } catch { setNote('set substream to H.264'); }
        } else {
          setNote('stream unavailable');
          try { hls?.destroy(); } catch { /* gone */ }
          reattach = window.setTimeout(() => { setMode('none'); setMode('hls'); }, 3000);
        }
      });
      hls.loadSource(hlsUrl);
      hls.attachMedia(video);
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = hlsUrl;
      video.play().catch(() => setNote('tap to play'));
    } else {
      setNote('HLS not supported');
    }

    return () => {
      video.removeEventListener('playing', onPlaying);
      video.removeEventListener('waiting', onWaiting);
      if (reattach) window.clearTimeout(reattach);
      if (hls) { try { hls.destroy(); } catch { /* gone */ } }
    };
  }, [mode, hlsUrl]);

  return (
    <div className="cam-video-wrap">
      {mode === 'mjpeg' && <img ref={imgRef} className="cam-video" alt="" decoding="async" />}
      {mode === 'hls' && <video ref={videoRef} className="cam-video" muted playsInline autoPlay />}
      {(!playing || mode === 'none') && (
        <div className="cam-note">{mode === 'none' ? 'no stream configured' : note}</div>
      )}
    </div>
  );
}
