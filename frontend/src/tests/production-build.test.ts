// @vitest-environment node
import { execFile } from "node:child_process";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { expect, it } from "vitest";

it("excludes VITE_API_TOKEN from the production bundle", async () => {
  const token = "atelier-build-test-token-86c893c1";
  const outDir = await mkdtemp(join(tmpdir(), "atelier-frontend-build-"));
  try {
    await promisify(execFile)(process.execPath, ["node_modules/vite/bin/vite.js", "build", "--outDir", outDir], {
      env: { ...process.env, NODE_ENV: "production", VITE_API_TOKEN: token },
      timeout: 30_000,
    });

    const entries = await readdir(outDir, { withFileTypes: true, recursive: true });
    const files = entries.filter((entry) => entry.isFile());
    expect(files.some((file) => file.name.endsWith(".js"))).toBe(true);
    const contents = await Promise.all(files.map((file) => readFile(join(file.parentPath, file.name))));
    expect(files.filter((_, index) => contents[index].includes(token)).map((file) => file.name)).toEqual([]);
  } finally {
    await rm(outDir, { recursive: true, force: true });
  }
}, 35_000);
