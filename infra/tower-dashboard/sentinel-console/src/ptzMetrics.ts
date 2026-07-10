/** Map ONVIF GetStatus pan/tilt/zoom to operator-facing HUD values. */

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));

export interface PtzMetrics {
  az: number;
  el: number;
  zoom: number;
}

/**
 * Dahua / ONVIF cameras report normalized pan & tilt in roughly −1…1 and zoom
 * in 0…1 (wide → tele). Home is typically (0, 0, 0).
 */
export function onvifToMetrics(
  pan: number | null | undefined,
  tilt: number | null | undefined,
  zoom: number | null | undefined,
): PtzMetrics | null {
  if (pan == null && tilt == null && zoom == null) return null;

  const p = pan ?? 0;
  const t = tilt ?? 0;
  const z = zoom ?? 0;

  // Pan 0 = home heading; ±1 ≈ full sweep either side.
  const az = ((p * 180) + 360) % 360;
  // Tilt 0 = level; positive = up (ONVIF y+).
  const el = t * 30;
  // Zoom 0 = wide; map 0…1 → 1…8× for the optical readout.
  const zoomMag = clamp(1 + Math.max(0, z) * 7, 1, 8);

  return { az, el, zoom: zoomMag };
}

export function formatZoom(z: number): string {
  return `${z.toFixed(1)}×`;
}
