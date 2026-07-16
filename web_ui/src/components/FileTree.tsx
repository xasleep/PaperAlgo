import { FileText, Folder } from "lucide-react";
import type { RepoFileEntry } from "../api/types";

type FileTreeProps = {
  files: RepoFileEntry[];
  selectedPath: string | null;
  onSelectFile: (path: string) => void;
};

export default function FileTree({ files, selectedPath, onSelectFile }: FileTreeProps) {
  if (files.length === 0) {
    return <div className="empty-state">No repository files available.</div>;
  }

  return (
    <div className="file-tree" role="tree">
      {files.map((entry) => {
        const depth = Math.max(entry.path.split("/").length - 1, 0);
        const isFile = entry.type === "file";
        return (
          <button
            key={entry.path}
            className={`tree-row ${selectedPath === entry.path ? "selected" : ""}`}
            style={{ paddingLeft: `${12 + depth * 16}px` }}
            disabled={!isFile}
            onClick={() => isFile && onSelectFile(entry.path)}
            type="button"
          >
            {isFile ? <FileText size={15} /> : <Folder size={15} />}
            <span>{entry.name}</span>
            {typeof entry.size === "number" ? (
              <span className="tree-size">{formatBytes(entry.size)}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
