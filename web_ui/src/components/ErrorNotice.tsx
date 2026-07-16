import { AlertCircle } from "lucide-react";
import type { ApiError } from "../api/types";

type ErrorNoticeProps = {
  error: ApiError | null;
};

export default function ErrorNotice({ error }: ErrorNoticeProps) {
  if (!error) {
    return null;
  }

  return (
    <div className="error-notice" role="alert">
      <AlertCircle size={18} />
      <div>
        <strong>{error.code}</strong>
        <span>{error.message}</span>
      </div>
    </div>
  );
}
