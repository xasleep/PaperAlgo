import { Briefcase, PlusCircle, Power, Settings } from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";
import { NavLink } from "react-router-dom";
import { API_BASE_URL, api, toApiError } from "../api/client";

type AppShellProps = {
  children: ReactNode;
};

export default function AppShell({ children }: AppShellProps) {
  const [isStopping, setIsStopping] = useState(false);

  async function handleStop() {
    if (
      isStopping ||
      !window.confirm("Stop the local PaperAlgo API and SQLite Worker?")
    ) {
      return;
    }
    setIsStopping(true);
    try {
      await api.stopSystem();
    } catch (error) {
      setIsStopping(false);
      window.alert(toApiError(error).message);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">P2C</span>
          <div>
            <div className="brand-title">Paper2Code Console</div>
            <div className="brand-subtitle">{API_BASE_URL || "same-origin API"}</div>
          </div>
        </div>
        <nav className="nav-links" aria-label="Primary navigation">
          <NavLink to="/settings">
            <Settings size={16} />
            <span>Settings</span>
          </NavLink>
          <NavLink to="/jobs/new">
            <PlusCircle size={16} />
            <span>Create Job</span>
          </NavLink>
          <NavLink to="/jobs">
            <Briefcase size={16} />
            <span>Jobs</span>
          </NavLink>
          <button
            type="button"
            className="danger-button stop-system-button"
            onClick={handleStop}
            disabled={isStopping}
          >
            <Power size={16} />
            <span>{isStopping ? "Stopping…" : "Stop"}</span>
          </button>
        </nav>
      </header>
      <main className="main-content">{children}</main>
    </div>
  );
}
