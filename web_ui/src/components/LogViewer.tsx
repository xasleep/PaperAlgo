import { FileClock } from "lucide-react";

type LogViewerProps = {
  logs: string[];
  selectedLog: string | null;
  content: string | null;
  loading: boolean;
  onSelectLog: (file: string) => void;
};

export default function LogViewer({
  logs,
  selectedLog,
  content,
  loading,
  onSelectLog,
}: LogViewerProps) {
  return (
    <div className="log-viewer">
      <aside className="log-list" aria-label="Log files">
        {logs.length === 0 ? (
          <div className="empty-state">No log files available.</div>
        ) : (
          logs.map((log) => (
            <button
              className={`log-row ${selectedLog === log ? "selected" : ""}`}
              key={log}
              onClick={() => onSelectLog(log)}
              type="button"
            >
              <FileClock size={15} />
              <span>{log}</span>
            </button>
          ))
        )}
      </aside>
      <pre className="code-pane log-content">
        {loading ? "Loading log..." : content || "Select a log file."}
      </pre>
    </div>
  );
}
