import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import {
  Activity,
  CircleDollarSign,
  Clock3,
  Coins,
  KeyRound,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { ApiError, loadUsage, type UsageSeries, type UsageSummary } from "./api";

const UsageChart = lazy(() => import("./UsageChart"));

const number = new Intl.NumberFormat();
const KEY_STORAGE = "northgate.operatorKey";

function cost(value: number): string {
  return `$${(value / 1_000_000).toFixed(6)}`;
}

function timestamp(value: string): string {
  return new Date(value).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface AccessDialogProps {
  open: boolean;
  error: string;
  onConnect: (key: string) => void;
}

function AccessDialog({ open, error, onConnect }: AccessDialogProps) {
  const [value, setValue] = useState("");
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation">
      <form
        className="access-dialog"
        onSubmit={(event) => {
          event.preventDefault();
          onConnect(value);
        }}
      >
        <div className="dialog-title"><span className="brand-mark">N</span><h2>Operator access</h2></div>
        <label htmlFor="operatorKey">Operator key</label>
        <div className="input-wrap"><KeyRound size={16} aria-hidden="true" /><input id="operatorKey" type="password" value={value} onChange={(event) => setValue(event.target.value)} autoFocus autoComplete="current-password" required /></div>
        <p className="dialog-error">{error}</p>
        <button className="button primary" type="submit">Connect</button>
      </form>
    </div>
  );
}

function Metric({ label, value, icon: Icon }: { label: string; value: string; icon: typeof Activity }) {
  return <article><div className="metric-label"><Icon size={15} aria-hidden="true" /><span>{label}</span></div><strong>{value}</strong></article>;
}

export function App() {
  const [operatorKey, setOperatorKey] = useState(() => sessionStorage.getItem(KEY_STORAGE) ?? "");
  const [accessOpen, setAccessOpen] = useState(!operatorKey);
  const [accessError, setAccessError] = useState("");
  const [hours, setHours] = useState(24);
  const [interval, setInterval] = useState<"hour" | "day">("hour");
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [series, setSeries] = useState<UsageSeries | null>(null);
  const [status, setStatus] = useState<"connecting" | "online" | "error">("connecting");
  const [updated, setUpdated] = useState("Not loaded");

  const refresh = useCallback(async () => {
    if (!operatorKey) { setAccessOpen(true); return; }
    setStatus("connecting");
    try {
      const [nextSummary, nextSeries] = await loadUsage(operatorKey, hours, interval);
      setSummary(nextSummary);
      setSeries(nextSeries);
      setUpdated(`Updated ${new Date().toLocaleTimeString()}`);
      setStatus("online");
      setAccessError("");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Request failed";
      setStatus("error");
      setAccessError(message);
      if (error instanceof ApiError && (error.status === 401 || error.status === 503)) {
        if (error.status === 401) sessionStorage.removeItem(KEY_STORAGE);
        setAccessOpen(true);
      }
    }
  }, [operatorKey, hours, interval]);

  useEffect(() => { void refresh(); }, [refresh]);

  const errorRate = summary?.requests ? (summary.error_requests / summary.requests) * 100 : 0;
  return (
    <>
      <header className="topbar">
        <div className="brand"><span className="brand-mark">N</span><div><h1>Northgate</h1><p>Operations</p></div></div>
        <div className="topbar-actions">
          <span className={`status ${status}`}><span />{status === "online" ? "Online" : status === "error" ? "Unavailable" : "Connecting"}</span>
          <button className="button secondary" type="button" onClick={() => setAccessOpen(true)}><KeyRound size={15} aria-hidden="true" />Access</button>
          <button className="button primary" type="button" onClick={() => void refresh()}><RefreshCw size={15} aria-hidden="true" />Refresh</button>
        </div>
      </header>
      <main>
        <section className="toolbar" aria-label="Dashboard filters">
          <div className="field"><label htmlFor="range">Range</label><select id="range" value={hours} onChange={(event) => setHours(Number(event.target.value))}><option value={24}>Last 24 hours</option><option value={168}>Last 7 days</option><option value={720}>Last 30 days</option></select></div>
          <div className="field"><span className="label">Bucket</span><div className="segments" role="group" aria-label="Time bucket"><button type="button" className={interval === "hour" ? "active" : ""} onClick={() => setInterval("hour")}>Hour</button><button type="button" className={interval === "day" ? "active" : ""} onClick={() => setInterval("day")}>Day</button></div></div>
          <p className="updated">{updated}</p>
        </section>
        <section className="metrics" aria-label="Usage summary">
          <Metric label="Requests" value={summary ? number.format(summary.requests) : "-"} icon={Activity} />
          <Metric label="Total tokens" value={summary ? number.format(summary.total_tokens) : "-"} icon={Coins} />
          <Metric label="Cost" value={summary ? cost(summary.cost_microusd) : "-"} icon={CircleDollarSign} />
          <Metric label="Average latency" value={summary?.average_latency_ms == null ? "-" : `${number.format(summary.average_latency_ms)} ms`} icon={Clock3} />
          <Metric label="Error rate" value={`${errorRate.toFixed(2)}%`} icon={ShieldAlert} />
        </section>
        <section className="chart-section">
          <div className="section-heading"><div><h2>Traffic and token volume</h2><p>{series ? `${new Date(series.start).toLocaleString()} - ${new Date(series.end).toLocaleString()}` : "-"}</p></div><div className="legend"><span className="requests-key">Requests</span><span className="tokens-key">Tokens</span></div></div>
          <Suspense fallback={<div className="chart-wrap"><div className="empty-chart">Loading chart</div></div>}><UsageChart points={series?.points ?? []} /></Suspense>
        </section>
        <section className="table-section">
          <div className="section-heading"><h2>Usage buckets</h2></div>
          <div className="table-wrap"><table><thead><tr><th>Timestamp</th><th>Requests</th><th>Tokens</th><th>Cost</th><th>Avg latency</th></tr></thead><tbody>{series?.points.length ? series.points.slice().reverse().map((point) => <tr key={point.timestamp}><td>{timestamp(point.timestamp)}</td><td>{number.format(point.requests)}</td><td>{number.format(point.total_tokens)}</td><td>{cost(point.cost_microusd)}</td><td>{point.average_latency_ms == null ? "-" : `${number.format(point.average_latency_ms)} ms`}</td></tr>) : <tr><td colSpan={5} className="empty">No usage in this range</td></tr>}</tbody></table></div>
        </section>
      </main>
      <AccessDialog open={accessOpen} error={accessError} onConnect={(value) => { sessionStorage.setItem(KEY_STORAGE, value); setOperatorKey(value); setAccessOpen(false); }} />
    </>
  );
}
