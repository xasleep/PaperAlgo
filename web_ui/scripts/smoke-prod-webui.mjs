import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import net from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webUiRoot = join(scriptDir, "..");
const repoRoot = join(webUiRoot, "..");
const distIndex = join(webUiRoot, "dist", "index.html");
const htmlRoutes = ["/", "/settings", "/jobs", "/jobs/new", "/jobs/smoke-job"];

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function findFreePort(startPort = 8210) {
  for (let port = startPort; port < startPort + 100; port += 1) {
    const free = await new Promise((resolve) => {
      const server = net.createServer();
      server.once("error", () => resolve(false));
      server.once("listening", () => {
        server.close(() => resolve(true));
      });
      server.listen(port, "127.0.0.1");
    });
    if (free) return port;
  }
  throw new Error("No free local port found for production smoke check.");
}

async function waitForFastApi(baseUrl) {
  const deadline = Date.now() + 25_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/health`, {
        headers: { Accept: "application/json" },
      });
      if (response.ok) return;
    } catch {
      await wait(250);
    }
  }
  throw new Error(`FastAPI did not become ready at ${baseUrl}`);
}

async function assertHtmlRoute(baseUrl, route) {
  const response = await fetch(`${baseUrl}${route}`, {
    headers: { Accept: "text/html" },
  });
  if (!response.ok) {
    throw new Error(`${route} returned HTTP ${response.status}`);
  }
  const html = await response.text();
  if (!html.includes('<div id="root"></div>') || !html.includes("/assets/")) {
    throw new Error(`${route} did not return the built React shell`);
  }
}

async function assertApiRoutes(baseUrl) {
  const health = await fetch(`${baseUrl}/health`, {
    headers: { Accept: "application/json" },
  });
  const healthBody = await health.json();
  if (!health.ok || healthBody.status !== "ok") {
    throw new Error("/health did not return expected JSON");
  }

  const jobs = await fetch(`${baseUrl}/jobs`, {
    headers: { Accept: "application/json" },
  });
  const contentType = jobs.headers.get("content-type") || "";
  const jobsBody = await jobs.json();
  if (!jobs.ok || !contentType.includes("application/json") || !Array.isArray(jobsBody.jobs)) {
    throw new Error("/jobs did not return JobList JSON for application/json");
  }
}

async function stopProcessTree(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    await new Promise((resolve) => {
      const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
      killer.on("close", resolve);
      killer.on("error", resolve);
    });
    return;
  }
  child.kill("SIGTERM");
}

if (!existsSync(distIndex)) {
  console.error("web_ui/dist/index.html is missing. Please run npm run build before npm run smoke:prod.");
  process.exit(1);
}

const pythonPath =
  process.platform === "win32"
    ? join(repoRoot, ".venv", "Scripts", "python.exe")
    : join(repoRoot, ".venv", "bin", "python");
const pythonCommand = existsSync(pythonPath) ? pythonPath : "python";
const port = await findFreePort();
const baseUrl = `http://127.0.0.1:${port}`;
const server = spawn(
  pythonCommand,
  ["-m", "uvicorn", "web_api.main:app", "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning"],
  {
    cwd: repoRoot,
    env: {
      ...process.env,
      PYTHONPATH: repoRoot,
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  },
);

let output = "";
server.stdout.on("data", (chunk) => {
  output += chunk.toString();
});
server.stderr.on("data", (chunk) => {
  output += chunk.toString();
});

try {
  await waitForFastApi(baseUrl);
  for (const route of htmlRoutes) {
    await assertHtmlRoute(baseUrl, route);
  }
  await assertApiRoutes(baseUrl);
  console.log(`Production smoke passed at ${baseUrl}: ${htmlRoutes.join(", ")}, /health, /jobs`);
} catch (error) {
  console.error(output);
  console.error(error);
  process.exitCode = 1;
} finally {
  await stopProcessTree(server);
}
