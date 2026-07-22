import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { UsagePoint } from "./api";

function timestamp(value: string): string {
  return new Date(value).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function UsageChart({ points }: { points: UsagePoint[] }) {
  const data = useMemo(
    () => points.map((point) => ({ ...point, label: timestamp(point.timestamp) })),
    [points],
  );
  return (
    <div className="chart-wrap">
      {data.length ? (
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 12, right: 18, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="#e2e7e4" vertical={false} />
            <XAxis
              dataKey="label"
              stroke="#74807a"
              tickLine={false}
              axisLine={false}
              minTickGap={28}
              tick={{ fontSize: 11 }}
            />
            <YAxis
              yAxisId="requests"
              stroke="#74807a"
              tickLine={false}
              axisLine={false}
              width={42}
              tick={{ fontSize: 11 }}
              allowDecimals={false}
            />
            <YAxis yAxisId="tokens" orientation="right" hide />
            <Tooltip
              contentStyle={{
                border: "1px solid #cbd3cf",
                borderRadius: 5,
                boxShadow: "0 8px 24px rgba(25,35,30,.12)",
                fontSize: 12,
              }}
            />
            <Line
              yAxisId="requests"
              type="monotone"
              dataKey="requests"
              name="Requests"
              stroke="#20805d"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
            />
            <Line
              yAxisId="tokens"
              type="monotone"
              dataKey="total_tokens"
              name="Tokens"
              stroke="#356da5"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
            />
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div className="empty-chart">No usage in this range</div>
      )}
    </div>
  );
}
