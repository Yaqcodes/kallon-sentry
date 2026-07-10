import { colors, font } from '../tokens';

interface Props {
  value: number; // 5–100 (%)
  onChange: (value: number) => void;
  accent: string;
}

/** Maps UI speed % to ONVIF continuous-move velocity (0.05–1.0). */
export function speedToVelocity(pct: number): number {
  const clamped = Math.max(5, Math.min(100, pct));
  return 0.05 + (clamped / 100) * 0.95;
}

/**
 * PTZ jog speed. Tap nudge distance scales with speed × fixed pulse duration;
 * hold on the pad repeats short pulses while pressed.
 */
export default function PtzSpeedSlider({ value, onChange, accent }: Props) {
  return (
    <div className="ptz-speed">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span style={{ fontFamily: font.mono, fontSize: 10, letterSpacing: '.24em', color: colors.textFaint }}>
          DRIVE SPEED
        </span>
        <span style={{ fontFamily: font.mono, fontSize: 11, color: colors.textBright, letterSpacing: '.08em' }}>
          {value}%
        </span>
      </div>
      <input
        type="range"
        className="ptz-speed-slider"
        min={5}
        max={100}
        step={5}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-label="PTZ drive speed"
        title="Slower for fine aim, faster for sweeping"
        style={{ ['--accent' as string]: accent }}
      />
      <div style={{ fontFamily: font.mono, fontSize: 9, letterSpacing: '.06em', color: colors.textFaint }}>
        Slower = finer moves
      </div>
    </div>
  );
}
