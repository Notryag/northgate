export const numberFormat = new Intl.NumberFormat();

export function cost(value: number | null | undefined): string {
  return value == null ? "-" : `$${(value / 1_000_000).toFixed(6)}`;
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "-";
  return new Date(value).toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function duration(value: number | null | undefined): string {
  return value == null ? "-" : `${numberFormat.format(value)} ms`;
}
