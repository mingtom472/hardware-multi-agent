import { cpSync, existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const output = join(root, ".next", "standalone");
if (!existsSync(join(output, "server.js"))) {
  console.error("Production build missing. Run npm run build first.");
  process.exit(1);
}
cpSync(join(root, ".next", "static"), join(output, ".next", "static"), { recursive: true });
if (existsSync(join(root, "public"))) cpSync(join(root, "public"), join(output, "public"), { recursive: true });
process.env.HOSTNAME = process.env.FRONTEND_HOST ?? "0.0.0.0";
await import(pathToFileURL(join(output, "server.js")).href);
