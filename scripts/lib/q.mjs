// Runs SQL against LEADS_DB_URL and returns psql's output.
//
// Exists because execFileSync embeds its whole argv in any Error it throws, and the
// argv here contains the connection string -- so one malformed query spills the DB
// password into the transcript. This reports psql's stderr alone and never the
// command. (The URL is still in argv, hence visible to `ps`, same as every other
// script in this repo; that is a separate problem from leaking it into output.)
//
// SQL is fed on stdin rather than via -c so multi-statement scripts and quoting
// both stop being a hazard.

import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { loadEnv } from "./env.mjs";

const ROOT = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));

export function q(sql, { env = null, args = [] } = {}) {
  const e = env || loadEnv(ROOT);
  try {
    return execFileSync("/usr/bin/psql", ["-X", "-v", "ON_ERROR_STOP=1", e.LEADS_DB_URL, ...args, "-f", "-"], {
      input: sql,
      encoding: "utf8",
      maxBuffer: 1024 * 1024 * 512,
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch (err) {
    const msg = (err.stderr || "").toString().trim() || "failed";
    throw new Error(`psql: ${msg}`);
  }
}

/** Convenience for one-off inspection: q() the SQL and print it. */
export function show(label, sql, opts) {
  console.log(`### ${label}`);
  try { console.log(q(sql, opts)); } catch (err) { console.log(err.message + "\n"); }
}
