import { readdir, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const distDir = join(scriptDir, "..", "dist");
const forbidden = "http://localhost:8000";
const checkedFiles = [];

async function listFiles(dirPath) {
  const entries = await readdir(dirPath, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const entryPath = join(dirPath, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await listFiles(entryPath)));
    } else {
      files.push(entryPath);
    }
  }
  return files;
}

try {
  const files = await listFiles(distDir);
  for (const file of files) {
    const content = await readFile(file, "utf-8");
    checkedFiles.push(file);
    if (content.includes(forbidden)) {
      throw new Error(`${forbidden} found in ${file}`);
    }
  }
  console.log(
    `Same-origin production bundle verified: ${checkedFiles.length} files checked, no ${forbidden}.`,
  );
} catch (error) {
  if (error?.code === "ENOENT") {
    console.error(`dist directory is missing. Run npm run build before this check.`);
  } else {
    console.error(error);
  }
  process.exitCode = 1;
}
