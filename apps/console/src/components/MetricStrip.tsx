import type { LucideIcon } from "lucide-react";

export interface MetricItem {
  label: string;
  value: string;
  icon: LucideIcon;
  tone?: "neutral" | "healthy" | "warning" | "error";
}

export function MetricStrip({ items }: { items: MetricItem[] }) {
  return (
    <section className="metric-strip" aria-label="Summary metrics">
      {items.map(({ label, value, icon: Icon, tone = "neutral" }) => (
        <div className={`metric-item ${tone}`} key={label}>
          <span className="metric-name"><Icon size={15} />{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </section>
  );
}
