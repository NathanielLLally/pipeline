// Webshare proxy pool, parsed once per run.
//
// The pool is datacenter IPs. Ordinary prospect websites fetch through it normally,
// but search engines and Meta refuse it at the ASN level -- see the "Corroborating
// people" section of docs/ARCHITECTURE.md for what was measured. Do not build a
// feature on top of this pool that depends on reaching those.
//
// Credentials are kept as separate fields and handed to curl via --proxy-user rather
// than embedded in a socks5://user:pass@host URL. Two reasons: curl 8.20 rejects the
// embedded form outright ("Unsupported proxy syntax"), and the embedded form echoes
// the password back in curl's own error messages.

import { execFileSync } from "node:child_process";

/**
 * Fetches the pool. `mode` selects which Webshare endpoint to ask for:
 *
 *   direct   - 100 distinct IPs, one per entry. The list is fixed: refetching returns
 *              exactly the same 100 addresses, so "refresh the pool" cannot clear a
 *              rate limit on its own. Waiting does.
 *   rotating - one gateway host on 100 ports, assigning a fresh exit IP per connection.
 *              Measured wider subnet spread than the direct list (12 ports produced 12
 *              exits across 6 /16s), which is the reason to reach for it when a run has
 *              been throttled; it was *not* measurably better than direct on a set of
 *              previously-failed sites once those had recovered.
 */
export function loadProxies(env, mode = "direct") {
  const base = env.PROXY_LIST_URL;
  if (!base) throw new Error("PROXY_LIST_URL is not set");
  // Webshare encodes the mode in the path; "backbone" is its name for the rotating
  // gateway. Anything else is passed through untouched.
  const url = mode === "rotating" ? base.replace("/direct/", "/backbone/") : base;

  let raw;
  try {
    raw = execFileSync("/usr/bin/curl", ["-fsSL", url], { encoding: "utf8", maxBuffer: 4 << 20 });
  } catch (err) {
    // Never echo argv: it holds the list URL, which is itself a credential.
    throw new Error(`proxy list fetch failed: ${(err.stderr || "").toString().trim() || "curl error"}`);
  }

  const proxies = raw
    .replace(/\r/g, "")
    .split("\n")
    .map((l) => l.split(":"))
    .filter((f) => f.length >= 4)
    .map(([host, port, user, pass]) => ({ host, port, user, pass }));

  if (proxies.length === 0) throw new Error("no proxies parsed from list");
  return proxies;
}

/** Round-robin picker. Shared across workers so no single IP takes the whole run. */
export function rotator(proxies) {
  let i = 0;
  return () => proxies[i++ % proxies.length];
}
