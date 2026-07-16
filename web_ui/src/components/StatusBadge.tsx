type StatusBadgeProps = {
  status?: string | null;
  processState?: string | null;
};

function toneFor(value: string): string {
  if (["completed", "finished"].includes(value)) return "success";
  if (["running", "active"].includes(value)) return "active";
  if (["queued", "starting", "none"].includes(value)) return "muted";
  if (["failed", "orphaned"].includes(value)) return "danger";
  if (["canceled", "detached"].includes(value)) return "warn";
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
