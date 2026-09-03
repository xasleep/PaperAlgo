import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webUiRoot = join(scriptDir, "..");
const repoRoot = join(webUiRoot, "..");
const distIndex = join(webUiRoot, "dist", "index.html");
const fakeApiKey = "pr08-fake-key-do-not-render";
const fakeEvalKey = "pr08-fake-eval-key-do-not-render";
const fakePrompt = "pr08 synthetic prompt must not appear";

let tempRoot = "";
const sensitiveValues = new Set([fakeApiKey, fakeEvalKey, fakePrompt]);

function sanitize(value) {
  let text = String(value || "");
  for (const secret of sensitiveValues) {
    text = text.split(secret).join("<redacted>");
  }
  for (const path of [repoRoot, tempRoot].filter(Boolean)) {
    text = text.split(path).join("<local-path>");
    text = text.split(path.replace(/\\/g, "/")).join("<local-path>");
  }
  text = text.replace(/[A-Za-z]:[\\/][^\s"'`<>]+/g, "<local-path>");
  text = text.replace(/\/(?:home|tmp|var|mnt|opt)\/[^\s"'`<>]+/g, "<local-path>");
  return text.slice(-4000);
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function findFreePort(startPort = 8320) {
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
  throw new Error("No free loopback port found for fake E2E.");
}

async function waitForFastApi(baseUrl) {
  const deadline = Date.now() + 25_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/api/v1/health`, {
        headers: { Accept: "application/json" },
      });
      if (response.ok) return;
    } catch {
      await wait(250);
    }
  }
  throw new Error("FastAPI did not become ready for fake E2E.");
}

function commandOnPath(command) {
  const lookup = process.platform === "win32" ? "where.exe" : "which";
  const result = spawnSync(lookup, [command], {
    encoding: "utf-8",
    windowsHide: true,
  });
  if (result.status !== 0) return "";
  return String(result.stdout || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean) || "";
}

function findBrowserExecutable() {
  const candidates = [
    process.env.CHROME_PATH,
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    commandOnPath("google-chrome"),
    commandOnPath("chromium-browser"),
    commandOnPath("chromium"),
    commandOnPath("microsoft-edge"),
  ].filter(Boolean);
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) {
    throw new Error("No local Chromium-family browser found. Set CHROME_PATH for fake E2E.");
  }
  return found;
}

function pythonCommand() {
  const venvPython =
    process.platform === "win32"
      ? join(repoRoot, ".venv", "Scripts", "python.exe")
      : join(repoRoot, ".venv", "bin", "python");
  return existsSync(venvPython) ? venvPython : "python";
}

function fakeRegistryPayload() {
  const baseModel = {
    base_url: null,
    base_url_env: "FAKE_BASE_URL",
    api_key_env: "FAKE_API_KEY",
    max_n: 1,
    context_window: 100000,
    max_output_tokens: 1024,
    json_schema_support: true,
    usage_support: true,
    cache_token_support: false,
    timeout_seconds: 5,
    max_retries: 0,
    max_concurrency: 1,
    pricing: {
      status: "configured",
      currency: "USD",
      input_per_million: 1,
      cached_input_per_million: null,
      output_per_million: 2,
      effective_date: "2026-09-03",
    },
    request_options: {},
    fallback_model_ids: [],
  };
  return {
    version: 1,
    providers: {
      fake: {
        models: {
          "fake-chat": { ...baseModel, fallback_model_ids: ["fake-eval"] },
          "fake-eval": baseModel,
        },
      },
    },
  };
}

function serverEnvironment({ dbPath, localDir, runsDir, registryPath }) {
  const env = {
    ...process.env,
    CI: "true",
    JOB_RUNTIME: "sqlite",
    PAPER2CODE_DB_PATH: dbPath,
    PAPER2CODE_LOCAL_DIR: localDir,
    PAPER2CODE_RUNS_DIR: runsDir,
    PAPER2CODE_PROVIDER_REGISTRY_PATH: registryPath,
    PYTHONIOENCODING: "utf-8",
    PYTHONPATH: repoRoot,
    PYTHONUTF8: "1",
  };
  for (const key of Object.keys(env)) {
    if (/^(OPENAI|DEEPSEEK|QWEN|KIMI|MOONSHOT|ANTHROPIC|REPRODUCE|EVAL)_/.test(key)) {
      delete env[key];
    }
  }
  return env;
}

async function runPythonHelper(env, jobId, action) {
  const helper = String.raw`
import os
import sqlite3
import sys
from pathlib import Path

from web_api import config
from web_api.database import connect_database, utc_now
from web_api.job_repository import JobRepository

job_id = sys.argv[1]
action = sys.argv[2]
worker_id = "fake-e2e-worker"
instance_token = "fake-e2e-token"
db_path = os.environ["PAPER2CODE_DB_PATH"]
repo = JobRepository(db_path)

def latest_job():
    return repo.get_job(job_id)

def ensure_artifacts():
    run_dir = config.RUNS_DIR / job_id
    repo_dir = run_dir / "repo"
    results_dir = run_dir / "results"
    logs_dir = run_dir / "logs"
    repo_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "main.py").write_text("print('fake pipeline artifact')\n", encoding="utf-8")
    (results_dir / "summary.txt").write_text("synthetic result only\n", encoding="utf-8")
    (logs_dir / "pipeline.log").write_text("fake pipeline progress\n", encoding="utf-8")

def seed_costs():
    now = utc_now()
    with connect_database(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE jobs
            SET cost_budget_policy = 'hard',
                cost_budget_currency = 'USD',
                cost_budget_amount = '1.00'
            WHERE job_id = ?
            """,
            (job_id,),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO remote_call_ledger (
                job_id, logical_call_id, attempt_id, stage, stage_attempt,
                repair_attempt, recovery_attempt, provider_id, model_id,
                request_sequence, retry_sequence, fallback_sequence,
                pricing_contract_version, pricing_contract_fingerprint,
                pricing_status, currency, cost_status, cost_amount,
                input_tokens, output_tokens, cached_input_tokens,
                reasoning_tokens, total_tokens, status, started_at,
                completed_at, event_time, error_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "fake-e2e-logical",
                "fake-e2e-attempt",
                "coding",
                1,
                None,
                0,
                "fake",
                "fake-chat",
                1,
                0,
                0,
                "1",
                "0" * 64,
                "configured",
                "USD",
                "actual",
                "0.000003",
                2,
                1,
                None,
                None,
                3,
                "completed",
                now,
                now,
                now,
                None,
                now,
            ),
        )
        connection.commit()

def complete_latest_command(command_type, result_code):
    now = utc_now()
    with connect_database(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT id FROM job_commands
            WHERE job_id = ? AND command_type = ?
            ORDER BY id DESC LIMIT 1
            """,
            (job_id, command_type),
        ).fetchone()
        if row is None:
            raise RuntimeError("expected command was not recorded")
        connection.execute(
            """
            UPDATE job_commands
            SET status = 'completed',
                request_status = 'accepted',
                error_code = NULL,
                rejection_code = NULL,
                result_code = ?,
                completed_at = ?,
                updated_at = ?,
                version = version + 1
            WHERE id = ?
            """,
            (result_code, now, now, row["id"]),
        )
        connection.commit()
        return int(row["id"])

def acquire_lease():
    if not repo.acquire_worker_lease(worker_id, instance_token, lease_seconds=30):
        raise RuntimeError("fake worker lease is unavailable")

if action == "progress":
    job = latest_job()
    if job["execution_status"] == "queued":
        job = repo.transition_job(
            job_id,
            expected_version=int(job["version"]),
            execution_status="running",
            source="pipeline_adapter",
        )
    ensure_artifacts()
    seed_costs()
    job = latest_job()
    repo.record_job_event(
        job_id,
        event_type="job.status_changed",
        source="pipeline_adapter",
        job_version=int(job["version"]),
        execution_status=job["execution_status"],
        evaluation_status=job["evaluation_status"],
        quality_status=job["quality_status"],
        payload={"current_stage": "coding", "stage": "coding"},
    )
elif action == "cancel-applied":
    job = latest_job()
    if job["execution_status"] == "running":
        job = repo.transition_job(
            job_id,
            expected_version=int(job["version"]),
            execution_status="canceled",
            source="worker",
        )
    command_id = complete_latest_command("cancel", "canceled")
    job = latest_job()
    repo.record_job_event(
        job_id,
        event_type="job.canceled",
        source="worker",
        job_version=int(job["version"]),
        execution_status=job["execution_status"],
        evaluation_status=job["evaluation_status"],
        quality_status=job["quality_status"],
        payload={
            "command_id": command_id,
            "command_type": "cancel",
            "command_status": "completed",
        },
    )
elif action == "retry-applied":
    acquire_lease()
    command = repo.apply_next_retry_command(worker_id=worker_id, instance_token=instance_token)
    if command is None:
        raise RuntimeError("retry command was not applied")
elif action == "complete-rejected":
    job = latest_job()
    if job["execution_status"] == "queued":
        job = repo.transition_job(
            job_id,
            expected_version=int(job["version"]),
            execution_status="running",
            source="pipeline_adapter",
        )
    job = latest_job()
    if job["execution_status"] == "running":
        repo.transition_job(
            job_id,
            expected_version=int(job["version"]),
            execution_status="completed",
            evaluation_status="completed",
            quality_status="rejected",
            source="pipeline_adapter",
        )
elif action == "repair-applied":
    acquire_lease()
    command = repo.apply_next_repair_command(worker_id=worker_id, instance_token=instance_token)
    if command is None:
        raise RuntimeError("repair command was not applied")
else:
    raise RuntimeError("unknown helper action")

print("ok")
`;
  const child = spawn(pythonCommand(), ["-c", helper, jobId, action], {
    cwd: repoRoot,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += chunk.toString();
  });
  child.stderr.on("data", (chunk) => {
    output += chunk.toString();
  });
  const exitCode = await new Promise((resolve) => {
    child.on("close", resolve);
    child.on("error", () => resolve(1));
  });
  if (exitCode !== 0) {
    throw new Error(`fake helper failed during ${action}: ${sanitize(output)}`);
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

async function waitForBodyText(page, text, timeout = 10_000) {
  await page.waitForFunction(
    (expected) => document.body.innerText.includes(expected),
    text,
    { timeout },
  );
}

async function assertBodyDoesNotLeak(page) {
  const body = (await page.textContent("body")) || "";
  for (const secret of sensitiveValues) {
    assert(!body.includes(secret), "sensitive fake E2E value rendered in the browser");
  }
}

async function clickButton(page, label) {
  await page.getByRole("button", { name: new RegExp(label, "i") }).click();
}

async function main() {
  if (!existsSync(distIndex)) {
    throw new Error("Built WebUI is missing. Run npm run build before fake E2E.");
  }

  tempRoot = await mkdtemp(join(tmpdir(), "paper2code-e2e-"));
  const localDir = join(tempRoot, "local");
  const runsDir = join(tempRoot, "runs");
  const dbPath = join(localDir, "paper2code.db");
  const registryPath = join(tempRoot, "providers.fake.json");
  const pdfPath = join(tempRoot, "paper.pdf");
  await mkdir(localDir, { recursive: true });
  await mkdir(runsDir, { recursive: true });
  await writeFile(registryPath, JSON.stringify(fakeRegistryPayload()), "utf-8");
  await writeFile(pdfPath, "%PDF-1.4\n% fake paper\n", "utf-8");

  const env = serverEnvironment({ dbPath, localDir, runsDir, registryPath });
  const port = await findFreePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const server = spawn(
    pythonCommand(),
    [
      "-m",
      "uvicorn",
      "web_api.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--log-level",
      "warning",
    ],
    {
      cwd: repoRoot,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  let serverOutput = "";
  server.stdout.on("data", (chunk) => {
    serverOutput += chunk.toString();
  });
  server.stderr.on("data", (chunk) => {
    serverOutput += chunk.toString();
  });

  let browser = null;
  try {
    await waitForFastApi(baseUrl);
    browser = await chromium.launch({
      executablePath: findBrowserExecutable(),
      headless: true,
    });
    const page = await browser.newPage();
    page.on("console", (message) => {
      if (message.type() === "error") {
        serverOutput += `\nBrowser console error: ${message.text()}`;
      }
    });

    await page.goto(`${baseUrl}/settings`, { waitUntil: "domcontentloaded" });
    await waitForBodyText(page, "fake-chat");
    const providers = await page.evaluate(async () => {
      const response = await fetch("/api/v1/providers", { headers: { Accept: "application/json" } });
      return response.json();
    });
    const providerText = JSON.stringify(providers).toLowerCase();
    assert(providerText.includes("fake-chat"), "provider/model discovery did not include the fake model");
    assert(!providerText.includes("api_key"), "provider discovery leaked API key metadata");
    assert(!providerText.includes("base_url"), "provider discovery leaked base URL metadata");

    await page.locator("fieldset").nth(0).locator('input[type="password"]').fill(fakeApiKey);
    await page.locator("fieldset").nth(0).locator('input[type="text"]').fill("https://fake.provider/v1");
    await page.locator("fieldset").nth(1).locator('input[type="password"]').fill(fakeEvalKey);
    await page.locator("fieldset").nth(1).locator('input[type="text"]').fill("https://fake.provider/v1");
    await clickButton(page, "Save Settings");
    await waitForBodyText(page, "Settings saved");
    await assertBodyDoesNotLeak(page);
    const settingsStatus = await page.evaluate(async () => {
      const response = await fetch("/api/v1/settings/status", { headers: { Accept: "application/json" } });
      return response.json();
    });
    const statusText = JSON.stringify(settingsStatus);
    assert(settingsStatus.configured === true, "settings status was not configured");
    assert(!statusText.includes(fakeApiKey) && !statusText.includes(fakeEvalKey), "settings status leaked secrets");

    await page.goto(`${baseUrl}/jobs/new`, { waitUntil: "domcontentloaded" });
    await page.locator('input[type="file"]').setInputFiles(pdfPath);
    await page.locator("fieldset").nth(0).locator('input[type="text"]').fill("pr08-fake-e2e");
    await page.locator('input[type="number"]').first().fill("1");
    await page.locator('input[type="number"]').nth(1).fill("0");
    await clickButton(page, "Create Job");
    await page.waitForURL((url) => /\/jobs\/(?!new$)[^/]+$/.test(url.pathname), {
      timeout: 10_000,
    });
    const jobId = decodeURIComponent(new URL(page.url()).pathname.split("/").pop() || "");
    assert(jobId.length > 0, "created job id was missing from the detail URL");
    await waitForBodyText(page, "queued");

    await runPythonHelper(env, jobId, "progress");
    await waitForBodyText(page, "running");
    await waitForBodyText(page, "coding");
    await waitForBodyText(page, "Actual USD 0.000003");
    await waitForBodyText(page, "Hard budget USD 1.00");
    await waitForBodyText(page, "open");

    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForBodyText(page, "running");
    await waitForBodyText(page, "Actual USD 0.000003");
    await clickButton(page, "Refresh");
    await waitForBodyText(page, "repo_ready");
    await waitForBodyText(page, "true");
    await page.getByRole("tab", { name: "repo" }).click();
    await waitForBodyText(page, "main.py");
    await page.getByRole("button", { name: /main\.py/i }).click();
    await waitForBodyText(page, "fake pipeline artifact");
    await page.getByRole("tab", { name: "logs" }).click();
    await waitForBodyText(page, "pipeline.log");

    await clickButton(page, "Cancel");
    await waitForBodyText(page, "pending");
    await runPythonHelper(env, jobId, "cancel-applied");
    await clickButton(page, "Refresh");
    await waitForBodyText(page, "canceled");
    await waitForBodyText(page, "applied");

    await clickButton(page, "Retry");
    await runPythonHelper(env, jobId, "retry-applied");
    await clickButton(page, "Refresh");
    await waitForBodyText(page, "queued");

    await runPythonHelper(env, jobId, "complete-rejected");
    await clickButton(page, "Refresh");
    await waitForBodyText(page, "repair_available");
    await clickButton(page, "Repair");
    await runPythonHelper(env, jobId, "repair-applied");
    await clickButton(page, "Refresh");
    await waitForBodyText(page, "Repair");
    await waitForBodyText(page, "applied");
    await assertBodyDoesNotLeak(page);

    console.log(
      "Fake-provider Playwright E2E passed: settings, discovery, create job, SSE/reload, REST refresh, artifacts, cancel, retry, repair, cost/budget.",
    );
  } catch (error) {
    const combined = `${error instanceof Error ? error.message : String(error)}\n${serverOutput}`;
    throw new Error(sanitize(combined));
  } finally {
    if (browser) {
      await browser.close();
    }
    await stopProcessTree(server);
    if (tempRoot) {
      await rm(tempRoot, { force: true, recursive: true });
    }
  }
}

try {
  await main();
} catch (error) {
  console.error(sanitize(error instanceof Error ? error.message : String(error)));
  process.exitCode = 1;
}
