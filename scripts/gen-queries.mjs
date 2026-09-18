#!/usr/bin/env node
// Generates a queries file (one keyword per line) for a given phase/service-category/metro,
// skipping the individual (geo_target, keyword) pairs already run per leads.search_log.
//
// Usage:
//   node scripts/gen-queries.mjs --phase prompt1 --service-category dog_training --metro "Los Angeles, CA" --out queries.txt
//   node scripts/gen-queries.mjs --phase prompt1 --service-category dog_training --out queries.txt   (all metros)
//   node scripts/gen-queries.mjs --phase prompt2 --service-category dog_training --force             (re-run covered pairs)

import { readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { loadEnv } from "./lib/env.mjs";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) {
      const key = argv[i].slice(2);
      const val = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : true;
      out[key] = val;
    }
  }
  return out;
}

// Coverage is per keyword, not per geo_target: a geo that has only seen the prompt1
// terms is still uncovered for the prompt2 specialist terms. The keyword ("<term> in
// <geo>") already encodes both halves uniquely, so the covered set is just the set of
// keywords already run. Rows with a NULL query predate the per-query search_log and
// are ignored -- counting them would mark every touched geo as covered for every
// term, which is exactly the bug this replaced.
function coveredKeywords(env, serviceCategory) {
  try {
    const out = execFileSync(
      "/usr/bin/psql",
      ["-X", "-t", "-A", env.LEADS_DB_URL, "-c",
        `select distinct query from leads.search_log
          where service_category = '${serviceCategory.replace(/'/g, "''")}' and query is not null`],
      { encoding: "utf8" }
    );
    return new Set(out.split("\n").map((s) => s.trim()).filter(Boolean));
  } catch (err) {
    console.error("Warning: could not query leads.search_log for coverage (treating nothing as covered):", err.message);
    return new Set();
  }
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const phase = args.phase || "unspecified";
  const serviceCategory = args["service-category"];
  const metroFilter = args.metro;
  const outPath = args.out || "queries.txt";
  const maxGeo = args["max-geo"] ? parseInt(args["max-geo"], 10) : Infinity;

  if (!serviceCategory) {
    console.error(
      "Usage: gen-queries.mjs --service-category <dog_training|daycare_boarding|grooming|dog_walking_petsitting>" +
      " [--metro \"City, ST\"] [--phase name] [--out path] [--max-geo N] [--force]"
    );
    process.exit(2);
  }

  const metros = JSON.parse(readFileSync(path.join(ROOT, "queries/metros.json"), "utf8")).metros;
  const terms = JSON.parse(readFileSync(path.join(ROOT, "queries/service_terms.json"), "utf8"))[serviceCategory];
  if (!terms) {
    console.error(`Unknown service category "${serviceCategory}". Known: ${Object.keys(JSON.parse(readFileSync(path.join(ROOT, "queries/service_terms.json"), "utf8"))).join(", ")}`);
    process.exit(2);
  }

  const env = loadEnv(ROOT);
  const covered = args.force ? new Set() : coveredKeywords(env, serviceCategory);

  const selectedMetros = metroFilter ? metros.filter((m) => m.metro === metroFilter) : metros;
  if (metroFilter && selectedMetros.length === 0) {
    console.error(`No metro named "${metroFilter}" in queries/metros.json`);
    process.exit(2);
  }

  // A geo counts against --max-geo only once it contributes at least one uncovered
  // term, so the cap limits work generated rather than geos merely inspected.
  const lines = [];
  const meta = [];
  const geoTargets = new Set();
  let skipped = 0;

  outer: for (const m of selectedMetros) {
    for (const geo of [m.core_city, ...m.suburbs]) {
      const pending = terms.filter((t) => !covered.has(`${t} in ${geo}`));
      skipped += terms.length - pending.length;
      if (pending.length === 0) continue;
      if (geoTargets.size >= maxGeo) break outer;
      geoTargets.add(geo);
      for (const term of pending) {
        const keyword = `${term} in ${geo}`;
        lines.push(keyword);
        meta.push({ keyword, geo_target: geo, service_category: serviceCategory, phase });
      }
    }
  }

  writeFileSync(outPath, lines.join("\n") + (lines.length ? "\n" : ""));
  const metaPath = args.meta || outPath.replace(/(\.[^./]+)?$/, ".meta.json");
  writeFileSync(metaPath, JSON.stringify(meta, null, 2));

  console.error(
    `[gen-queries] phase=${phase} service_category=${serviceCategory} metro=${metroFilter || "(all)"} ` +
    `-> ${lines.length} queries across ${geoTargets.size} geo_targets ` +
    `(${skipped} geo/term pairs already covered, skipped)${args.force ? " [--force: coverage ignored]" : ""} ` +
    `written to ${outPath} (meta: ${metaPath})`
  );
}

main();
