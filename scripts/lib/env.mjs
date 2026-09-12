import { readFileSync } from "node:fs";
import path from "node:path";

// Parses the project's .env, which is written to be `source`-d by bash: double-quoted
// (or unquoted) values may reference $VAR/${VAR} set earlier in the same file;
// single-quoted values are literal, exactly like bash.
export function loadEnv(root) {
  const envPath = path.join(root, ".env");
  const env = { ...process.env };
  for (const line of readFileSync(envPath, "utf8").split("\n")) {
    const m = line.match(/^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$/) || line.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!m) continue;
    let val = m[2];
    const singleQuoted = val.startsWith("'") && val.endsWith("'");
    const doubleQuoted = val.startsWith('"') && val.endsWith('"');
    if (singleQuoted || doubleQuoted) val = val.slice(1, -1);
    if (!singleQuoted) {
      val = val.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)/g, (_, a, b) => env[a || b] ?? "");
    }
    env[m[1]] = val;
  }
  return env;
}
