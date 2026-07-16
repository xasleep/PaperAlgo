import { Link, useLocation } from "react-router-dom";

export default function NotFoundPage() {
  const location = useLocation();

  return (
    <section className="page-stack">
      <div className="page-header">
        <div>
          <h1>Not Found</h1>
          <p className="mono-text">{location.pathname}</p>
        </div>
      </div>
      <div className="panel not-found-panel">
        <p>This WebUI route does not exist.</p>
        <div className="link-actions">
          <Link className="secondary-button" to="/jobs">
            Jobs
          </Link>
          <Link className="secondary-button" to="/jobs/new">
            Create Job
          </Link>
          <Link className="secondary-button" to="/settings">
            Settings
          </Link>
        </div>
      </div>
    </section>
  );
}
