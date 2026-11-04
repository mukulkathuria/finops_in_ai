import type { Unit } from "./api/types";

const MICRO = 1_000_000;
const MAX_LIMIT = 1_000_000_000_000_000;

/** micro-USD to dollars, exactly (integer arithmetic): $1.50, $0.000007. */
export function usd(micro: number): string {
  const sign = micro < 0 ? "-" : "";
  const abs = Math.abs(micro);
  const whole = Math.floor(abs / MICRO).toLocaleString("en-US");
  const fraction = String(abs % MICRO)
    .padStart(6, "0")
    .replace(/0+$/, "")
    .padEnd(2, "0");
  return `${sign}$${whole}.${fraction}`;
}

export function amount(value: number, unit: Unit): string {
  if (unit === "usd") return usd(value);
  return `${value.toLocaleString("en-US")} ${unit}`;
}

/** Parse a user-typed amount into the API's integer unit (micro-USD for dollars); null if bad. */
export function parseAmount(text: string, unit: Unit): number | null {
  const clean = text.trim().replace(/[$,\s]/g, "");
  if (unit === "usd") {
    const match = /^(\d+)(?:\.(\d{1,6}))?$/.exec(clean);
    if (!match) return null;
    const value = Number(match[1]) * MICRO + Number((match[2] ?? "").padEnd(6, "0"));
    return Number.isSafeInteger(value) && value <= MAX_LIMIT ? value : null;
  }
  if (!/^\d+$/.test(clean)) return null;
  const value = Number(clean);
  return Number.isSafeInteger(value) && value <= MAX_LIMIT ? value : null;
}

export function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "-";
}

/** The random tail of an id: UUIDv7 ids share their leading (timestamp) digits. */
export function shortId(id: string | null): string {
  return id ? id.slice(-8) : "-";
}

/** A part of a whole as a percentage ("12.5%"), or "-" when there is no whole. */
export function share(part: number, whole: number): string {
  return whole > 0 ? `${((100 * part) / whole).toFixed(1)}%` : "-";
}

/** A local calendar day as YYYY-MM-DD (UTC can fall on another day). */
export function localDay(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
