import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, toApiError } from "../api/client";
import type { ApiError, JobListItem } from "../api/types";
import ErrorNotice from "../components/ErrorNotice";
import StatusBadge from "../components/StatusBadge";

export default function JobsPage() {
  const [jobs, setJobs] = useState<JobListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  const loadJobs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listJobs();
      setJobs(response.jobs);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  return (
    <section className="page-stack">
      <div className="page-header">
        <div>
          <h1>Jobs</h1>
          <p>Recent local pipeline runs.</p>
        </div>
        <button className="secondary-button" onClick={loadJobs} type="button">
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>

      <ErrorNotice error={error} />

      <div className="panel table-panel">
        {jobs.length === 0 && !loading ? (
          <div className="empty-state">No jobs found.</div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>job_id</th>
                  <th>paper_name</th>
                  <th>status</th>
                  <th>process_state</th>
                  <th>cancelable</th>
                  <th>stage</th>
                  <th>updated_at</th>
                  <th>eval_score</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((job) => (
                  <tr key={job.job_id}>
                    <td>
                      <Link className="mono-link" to={`/jobs/${encodeURIComponent(job.job_id)}`}>
                        {job.job_id}
                      </Link>
                    </td>
                    <td>{job.paper_name || "-"}</td>
                    <td>
                      <StatusBadge status={job.status} />
                    </td>
                    <td>{job.process_state}</td>
                    <td>{String(job.cancelable)}</td>
                    <td>{job.stage || "-"}</td>
                    <td>{job.updated_at || "-"}</td>
                    <td>{job.eval_score ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {loading ? <div className="loading-row">Loading jobs...</div> : null}
      </div>
    </section>
  );
}
