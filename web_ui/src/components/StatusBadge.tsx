type StatusBadgeProps = {
  status?: string | null;
  processState?: string | null;
};

function toneFor(value: string): string {
  if (["completed", "finished"].includes(value)) return "success";
  if (["running", "active", "open", "connecting", "reconnecting", "resyncing"].includes(value)) return "active";
  if (["queued", "starting", "none", "idle", "closed", "pending", "skipped"].includes(value)) return "muted";
  if (["failed", "orphaned", "rejected", "repair_blocked_evaluation_failed"].includes(value)) return "danger";
  if (["canceled", "detached", "fallback", "repair_available"].includes(value)) return "warn";
  if (["accepted", "no_repair_needed", "recovery_completed"].includes(value)) return "success";
  if (["recovery_prepared", "recovery_running"].includes(value)) return "active";
  if (["recovery_failed"].includes(value)) return "danger";
  return "neutral";
}

export default function StatusBadge({ status, processState }: StatusBadgeProps) {
  const primary = status || processState || "unknown";
  const secondary = status && processState ? processState : null;
  const tone = toneFor(primary);

  return (
    <span className={`status-badge status-${tone}`}>
      <span>{primary}</span>
      {secondary ? <span className="status-sub">{secondary}</span> : null}
    </span>
  );
}
