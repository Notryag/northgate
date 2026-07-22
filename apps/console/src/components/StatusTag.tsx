import { Tag } from "antd";

const success = new Set(["succeeded", "completed", "ready", "hit"]);
const warning = new Set(["started", "pending", "retry", "processing", "degraded"]);
const error = new Set([
  "failed",
  "settlement_incomplete",
  "policy_rejected",
  "timeout",
  "transport_error",
  "cancelled",
]);

export function StatusTag({ value }: { value: string | null | undefined }) {
  const normalized = value ?? "unknown";
  const color = success.has(normalized)
    ? "success"
    : warning.has(normalized)
      ? "warning"
      : error.has(normalized)
        ? "error"
        : "default";
  return <Tag color={color}>{normalized.replaceAll("_", " ")}</Tag>;
}
