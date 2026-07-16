import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const host = "127.0.0.1";
const port = "5199";
const baseUrl = `http://${host}:${port}`;
const routes = ["/settings", "/jobs/new", "/jobs", "/jobs/smoke-job"];

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForServer() {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(baseUrl);
      if (response.ok) return;
    } catch {
      await wait(250);
    }
  }
  throw new Error(`Vite dev server did not become ready at ${baseUrl}`);
}

async function assertSpaRoute(route) {
  const response = await fetch(`${baseUrl}${route}`);
  if (!response.ok) {
    throw new Error(`${route} returned HTTP ${response.status}`);
  }
  const html = await response.text();
  if (!html.includes('<div id="root"></div>') || !html.includes("/src/main.tsx")) {
    throw new Error(`${route} did not return the Vite React shell`);
  }
}

const command = process.execPath;
const viteBin = fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url));
const server = spawn(
  command,
  [viteBin, "--host", host, "--port", port, "--strictPort"],
  {
    cwd: process.cwd(),
    env: {
      ...process.env,
      VITE_API_BASE_URL: "http://127.0.0.1:65535",
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
  await waitForServer();
  for (const route of routes) {
    await assertSpaRoute(route);
  }
  console.log(`Smoke routes passed: ${routes.join(", ")}`);
} catch (error) {
  console.error(output);
  console.error(error);
  process.exitCode = 1;
} finally {
  if (!server.killed) {
    server.kill();
  }
}
