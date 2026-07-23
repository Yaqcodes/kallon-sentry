import { useState, type PointerEvent } from 'react';
import type { CSSProperties } from 'react';
import { font } from '../tokens';

export type PanDir = 'up' | 'down' | 'left' | 'right';

interface Props {
  accent: string;
  size?: string;
  /** Pointer down — begin hold-to-jog */
  onPanStart: (dir: PanDir) => void;
  /** Pointer up / leave — end jog or complete tap */
  onPanEnd: () => void;
  onRecenter: () => void;
}

/**
 * Directional pad. Tap = one nudge; press-and-hold = continuous jog at the
 * speed set by the slider (parent repeats short ONVIF pulses).
 */
export default function PtzPad({ accent, size = '176px', onPanStart, onPanEnd, onRecenter }: Props) {
  const accentVar = { '--accent': accent } as CSSProperties;
  const [active, setActive] = useState<PanDir | null>(null);

  const bind = (dir: PanDir) => ({
    onPointerDown: (e: PointerEvent<HTMLButtonElement>) => {
      e.preventDefault();
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      setActive(dir);
      onPanStart(dir);
    },
    onPointerUp: (e: PointerEvent<HTMLButtonElement>) => {
      e.preventDefault();
      setActive(null);
      onPanEnd();
    },
    onPointerCancel: () => {
      setActive(null);
      onPanEnd();
    },
    onLostPointerCapture: () => {
      setActive(null);
      onPanEnd();
    },
    className: `pad-btn pad-${dir}${active === dir ? ' active' : ''}`,
    'aria-pressed': active === dir,
  });

  return (
    <div style={{ position: 'relative', width: size, height: size, flex: 'none', touchAction: 'none' }}>
      <div style={{ position: 'absolute', inset: 0, borderRadius: '50%', border: '1px solid #2b343d', background: 'radial-gradient(circle at 50% 42%,#161d23,#0f151a)', boxShadow: 'inset 0 2px 10px rgba(0,0,0,.5)' }} />
      <div style={{ position: 'absolute', inset: 14, borderRadius: '50%', border: '1px dashed #29323b', pointerEvents: 'none' }} />

      <button type="button" {...bind('up')} aria-label="Tilt up">▲</button>
      <button type="button" {...bind('down')} aria-label="Tilt down">▼</button>
      <button type="button" {...bind('left')} aria-label="Pan left">◀</button>
      <button type="button" {...bind('right')} aria-label="Pan right">▶</button>

      <button
        type="button"
        className="pad-center"
        onClick={onRecenter}
        title="Recenter camera"
        aria-label="Recenter camera"
        style={{ ...accentVar, fontFamily: font.display }}
      >
        <span style={{ fontSize: 20, lineHeight: 1 }}>⌖</span>
        <span style={{ fontWeight: 700, fontSize: 9, letterSpacing: '.1em', lineHeight: 1 }}>HOME</span>
      </button>
    </div>
  );
}
