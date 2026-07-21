const UTC1_OFFSET_MS = 60 * 60 * 1000;

export function formatClockUTC1(ms: number): string {
  const d = new Date(ms + UTC1_OFFSET_MS);
  return d.toISOString().slice(11, 19);
}

/** Human-readable UTC+1 timestamp for alert/recording lists. */
export function formatDateTimeUTC1(iso: string): string {
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  const d = new Date(parsed + UTC1_OFFSET_MS);
  return d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC+1';
}
