import type { CSSProperties } from 'react';
import { font } from '../tokens';

export type PanDir = 'up' | 'down' | 'left' | 'right';

interface Props {
  accent: string;
  size?: string;
  onPan: (dir: PanDir) => void;
  onRecenter: () => void;
}

/**
 * Directional pad. The four arrows drive the selected camera; the center
 * button recenters it to home. Arrows are press-and-hold in the console
 * (continuous move while pressed), matching a real PTZ jog control.
 */
export default function PtzPad({ accent, size = '176px', onPan, onRecenter }: Props) {
  const accentVar = { '--accent': accent } as CSSProperties;

  return (
    <div style={{ position: 'relative', width: size, height: size, flex: 'none' }}>
      <div style={{ position: 'absolute', inset: 0, borderRadius: '50%', border: '1px solid #2b343d', background: 'radial-gradient(circle at 50% 42%,#161d23,#0f151a)', boxShadow: 'inset 0 2px 10px rgba(0,0,0,.5)' }} />
      <div style={{ position: 'absolute', inset: 14, borderRadius: '50%', border: '1px dashed #29323b', pointerEvents: 'none' }} />

      <button className="pad-btn pad-up" onClick={() => onPan('up')} aria-label="Tilt up">▲</button>
      <button className="pad-btn pad-down" onClick={() => onPan('down')} aria-label="Tilt down">▼</button>
      <button className="pad-btn pad-left" onClick={() => onPan('left')} aria-label="Pan left">◀</button>
      <button className="pad-btn pad-right" onClick={() => onPan('right')} aria-label="Pan right">▶</button>

      <button
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
