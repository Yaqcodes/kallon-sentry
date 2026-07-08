// Palette + type, lifted straight from the Claude Design export so the
// React build stays visually identical to the approved design.

export const colors = {
  bg: '#0c1013',
  bgPanel: '#10171d',
  bgWell: '#0e1318',

  feedTop: '#1d262d',
  feedMid: '#141b21',
  feedBot: '#0e1318',

  line: '#232c34',
  line2: '#2b343d',
  bracketIdle: '#39434d',

  textBright: '#eef3f7',
  text: '#9aa6b0',
  textDim: '#8b97a1',
  textFaint: '#77828c',

  accent: '#ffffff', // white — ONLINE + selection (was mint green)
  standby: '#FFB020', // amber
  offline: '#FF5A5A', // red
  rec: '#ff3b3b',
  recText: '#ff6b6b',
} as const;

export const font = {
  display: "'Rajdhani', sans-serif",
  mono: "'JetBrains Mono', monospace",
} as const;
