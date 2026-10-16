import raw from '../data/journal.json';

export type Num = number | null | undefined;

export interface Night {
  id: string;
  label: string;
  utc_start: string | null;
  utc_end: string | null;
  exposure_s: Num;
  frames_total: Num;
  frames_kept: Num;
  qa_rejected: Num;
  scope_drop_rate: Num;
  minutes: Num;
  fwhm_arcsec: Num;
  sky_adu: Num;
  moon?: { illum_pct: Num; alt_deg_max: Num; alt_deg_mean: Num } | null;
  weather?: {
    cloud_pct_mean: Num;
    gust_mph_max: Num;
    humidity_pct_mean: Num;
    temp_minus_dew_c_min: Num;
  } | null;
  log_line?: string | null;
}

export interface Side {
  label: string;
  frames: Num;
  minutes: Num;
  fwhm_arcsec: Num;
  sky: string | null;
  snr?: Record<string, Num> | null;
  snr_headline: Num;
}

export interface Img {
  avif: string;
  webp: string;
  avif_1200: string;
  webp_1200: string;
  width: number;
  height: number;
}

export interface Stage {
  id: string;
  index: number;
  title: string;
  dates: string[];
  night_ids: string[];
  before: Side;
  after: Side;
  what_changed: string;
  what_it_did: string;
  images: { before: Img; after: Img; placeholder?: string | null };
  alignment?: { method: string | null; n_stars: Num; median_residual_px: Num } | null;
}

export interface ChartPoint {
  stage_id: string;
  label: string;
  minutes: Num;
  snr: Num;
}

export interface Journal {
  generated: string;
  nights: Night[];
  stages: Stage[];
  chart: ChartPoint[];
  hero_stage_id: string;
}

export const journal = raw as unknown as Journal;

export const isPlaceholder = JSON.stringify(raw).includes('PLACEHOLDER');

export const DASH = '—';

const isNum = (n: Num): n is number => typeof n === 'number' && Number.isFinite(n);

/** Format a number, or an em dash when it is missing. */
export function num(n: Num, digits = 0): string {
  if (!isNum(n)) return DASH;
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Number plus a unit, or a bare dash. */
export function unit(n: Num, u: string, digits = 0): string {
  return isNum(n) ? `${num(n, digits)}${u}` : DASH;
}

/** Fractions (0–1) and percentages (0–100) both come through as a percentage. */
export function pct(n: Num): string {
  if (!isNum(n)) return DASH;
  const v = Math.abs(n) <= 1 ? n * 100 : n;
  return `${num(v, 0)} %`;
}

export function arcsec(n: Num): string {
  return isNum(n) ? `${num(n, 1)}″` : DASH;
}

export function minutes(n: Num): string {
  if (!isNum(n)) return DASH;
  if (n >= 90) {
    const h = Math.floor(n / 60);
    const m = Math.round(n - h * 60);
    return `${h} h ${String(m).padStart(2, '0')} min`;
  }
  return `${num(n, 0)} min`;
}

function utcParts(iso: string | null | undefined): { date: string; time: string } | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const s = d.toISOString();
  return { date: s.slice(0, 10), time: s.slice(11, 16) };
}

/** "2026-09-07 21:10–02:40 UTC" */
export function utcSpan(start: string | null | undefined, end: string | null | undefined): string {
  const a = utcParts(start);
  const b = utcParts(end);
  if (!a && !b) return DASH;
  if (a && !b) return `${a.date} ${a.time} UTC`;
  if (!a && b) return `${b.date} –${b.time} UTC`;
  return `${a!.date} ${a!.time}–${b!.time} UTC`;
}

export function snrHeadline(s: Side): Num {
  if (isNum(s.snr_headline)) return s.snr_headline;
  return null;
}

/** Short readout for the slider handle: frames, minutes, SNR. */
export function readout(s: Side): string[] {
  return [
    s.label || DASH,
    `${num(s.frames)} fr · ${minutes(s.minutes)}`,
    `${arcsec(s.fwhm_arcsec)} · SNR ${num(snrHeadline(s), 1)}`,
  ];
}

export { isNum };
