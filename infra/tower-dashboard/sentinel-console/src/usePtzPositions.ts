import { useCallback, useEffect, useRef, useState } from 'react';
import { ptz } from './api';
import { onvifToMetrics, type PtzMetrics } from './ptzMetrics';

const POLL_SELECTED_MS = 1200;
const POLL_OTHERS_MS = 4000;

/**
 * Poll ONVIF PTZ status for each online camera so HUD metrics reflect the
 * real position instead of UI guesses.
 */
export function usePtzPositions(
  cameraNums: number[],
  readyPaths: Map<string, boolean>,
  selectedCamId: string,
) {
  const [positions, setPositions] = useState<Record<string, PtzMetrics>>({});
  const otherIdx = useRef(0);
  const bumpPoll = useRef<number | undefined>(undefined);

  const pollOne = useCallback(async (camNum: number, path: string) => {
    if (!readyPaths.get(path)) return;
    const res = await ptz('status', { camera: camNum });
    if (!res.ok || !res.result) return;
    const r = res.result as { pan?: number; tilt?: number; zoom?: number };
    const m = onvifToMetrics(r.pan, r.tilt, r.zoom);
    if (!m) return;
    const id = String(camNum).padStart(2, '0');
    setPositions((prev) => ({ ...prev, [id]: m }));
  }, [readyPaths]);

  const refreshSoon = useCallback(() => {
    if (bumpPoll.current) window.clearTimeout(bumpPoll.current);
    bumpPoll.current = window.setTimeout(() => {
      const id = selectedCamId || '01';
      const num = parseInt(id, 10) || 1;
      void pollOne(num, `cam${num}`);
    }, 350);
  }, [selectedCamId, pollOne]);

  // Selected camera: frequent poll
  useEffect(() => {
    if (!selectedCamId) return;
    let cancelled = false;
    let timer: number | undefined;
    const num = parseInt(selectedCamId, 10) || 1;
    const path = `cam${num}`;

    const tick = async () => {
      if (!cancelled) await pollOne(num, path);
      if (!cancelled) timer = window.setTimeout(tick, POLL_SELECTED_MS);
    };
    tick();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [selectedCamId, pollOne]);

  // Other online cameras: round-robin
  useEffect(() => {
    if (!cameraNums.length) return;
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      const nums = cameraNums.filter((n) => {
        const id = String(n).padStart(2, '0');
        return id !== selectedCamId && readyPaths.get(`cam${n}`);
      });
      if (nums.length) {
        otherIdx.current %= nums.length;
        const n = nums[otherIdx.current];
        otherIdx.current += 1;
        await pollOne(n, `cam${n}`);
      }
      if (!cancelled) timer = window.setTimeout(tick, POLL_OTHERS_MS);
    };
    tick();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [cameraNums, selectedCamId, readyPaths, pollOne]);

  return { positions, refreshSoon };
}
