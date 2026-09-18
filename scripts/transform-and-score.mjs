#!/usr/bin/env node
// Reads every result file in a batch's map-outputs dir (each file = one keyword's full
// array of scraped places, from create_search_job.py), tags each place with its source
// keyword/geo_target/service_category/phase (via the batch's meta.json sidecar), scores
// it against the ICP, and writes a staging CSV for `\copy` into leads.staging_businesses.

import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { scoreIcp, aboutOptionNames } from "./lib/score.mjs";

const STAGING_COLUMNS = [
  "place_id", "domain", "phone_normalized", "name_city_state_key",
  "name", "maps_url", "website", "phone", "address", "city", "state", "zip",
  "latitude", "longitude", "rating", "review_count", "primary_category", "additional_categories",
  "description", "hours", "price_range", "status",
  "service_category", "icp_score", "icp_tier", "qc_status",
  "query", "geo_target", "phase", "raw_scrape",
];

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

// Mirrors create_search_job.py's safe_filename() (ASCII \w is equivalent for our
// plain-ASCII generated keywords: lowercase, strip non-word/space/hyphen, spaces -> _).
function safeFilename(s) {
  return s.trim().toLowerCase().replace(/[^\w\s-]/g, "").replace(/\s+/g, "_").slice(0, 100);
}

function normalizeDomain(website) {
  if (!website) return null;
  try {
    const url = new URL(/^https?:\/\//i.test(website) ? website : `https://${website}`);
    return url.hostname.replace(/^www\./i, "").toLowerCase() || null;
  } catch {
    return null;
  }
}

function normalizePhone(phone) {
  if (!phone) return null;
  let digits = phone.replace(/\D/g, "");
  if (digits.length === 11 && digits.startsWith("1")) digits = digits.slice(1);
  return digits.length >= 7 ? digits : null;
}

function normalizeNameCityStateKey(name, city, state) {
  if (!name) return null;
  const clean = (s) =>
    (s || "")
      .toLowerCase()
      .replace(/\b(llc|inc|corp|co)\b\.?/g, "")
      .replace(/[^a-z0-9]+/g, "")
      .trim();
  const key = `${clean(name)}|${clean(city)}|${clean(state)}`;
  return clean(name) ? key : null;
}

function csvField(v) {
  if (v === null || v === undefined) return "";
  const s = String(v);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const batchDir = args["batch-dir"];
  const metaPath = args.meta;
  const outPath = args.out;

  if (!batchDir || !metaPath || !outPath) {
    console.error("Usage: transform-and-score.mjs --batch-dir <map-outputs/batch> --meta <batch/meta.json> --out <staging.csv>");
    process.exit(2);
  }

  const meta = JSON.parse(readFileSync(metaPath, "utf8"));
  // slug -> meta entry (keyword, geo_target, service_category, phase)
  const bySlug = new Map();
  for (const m of meta) {
    bySlug.set(safeFilename(m.keyword), m);
  }

  const files = readdirSync(batchDir).filter((f) => f.endsWith(".json"));
  const rows = [];
  let unmatchedFiles = 0;

  for (const file of files) {
    const base = file.replace(/\.json$/, "");
    // filename is `{job_id}-{slug}.json`; find which known slug this basename ends with.
    let matchedMeta = null;
    for (const [slug, m] of bySlug) {
      if (base === slug || base.endsWith(`-${slug}`)) {
        matchedMeta = m;
        break;
      }
    }
    if (!matchedMeta) {
      unmatchedFiles++;
      console.error(`Warning: no meta match for ${file}, skipping`);
      continue;
    }

    let places;
    try {
      places = JSON.parse(readFileSync(path.join(batchDir, file), "utf8"));
    } catch (e) {
      console.error(`Warning: could not parse ${file}: ${e.message}`);
      continue;
    }
    if (!Array.isArray(places)) continue;

    for (const p of places) {
      const website = p.web_site || null;
      const domain = normalizeDomain(website);
      const phone = p.phone || null;
      const phoneNormalized = normalizePhone(phone);
      const complete = p.complete_address || {};
      const city = complete.city || null;
      const state = complete.state || null;
      const zip = complete.postal_code || null;
      const name = p.title || null;
      const nameCityStateKey = normalizeNameCityStateKey(name, city, state);
      const categories = Array.isArray(p.categories) ? p.categories : p.category ? [p.category] : [];

      // serviceCategory comes back derived from the business's own Google categories,
      // not from the batch: a groomer surfaced by a boarding query is filed as
      // grooming. It falls back to the batch's category only for a rejected row, so
      // the NOT NULL column still has a value to record the rejection under.
      const { score, tier, qcStatus, serviceCategory } = scoreIcp({
        name,
        keyword: matchedMeta.keyword,
        primaryCategory: p.category,
        categories,
        description: p.description,
        about: aboutOptionNames(p.about),
        rating: p.review_rating,
        reviewCount: p.review_count,
        website,
        priceRange: p.price_range,
      });

      rows.push({
        place_id: p.place_id || null,
        domain,
        phone_normalized: phoneNormalized,
        name_city_state_key: nameCityStateKey,
        name,
        maps_url: p.link || null,
        website,
        phone,
        address: p.address || null,
        city,
        state,
        zip,
        latitude: p.latitude ?? null,
        longitude: p.longitude ?? null,
        rating: p.review_rating ?? null,
        review_count: p.review_count ?? null,
        primary_category: p.category || null,
        additional_categories: categories.join("|"),
        description: p.description || null,
        hours: p.open_hours ? JSON.stringify(p.open_hours) : null,
        price_range: p.price_range || null,
        status: p.status || null,
        service_category: serviceCategory || matchedMeta.service_category,
        icp_score: score,
        icp_tier: tier,
        qc_status: qcStatus,
        query: matchedMeta.keyword,
        geo_target: matchedMeta.geo_target,
        phase: matchedMeta.phase,
        raw_scrape: JSON.stringify(p),
      });
    }
  }

  const lines = [STAGING_COLUMNS.join(",")];
  for (const row of rows) {
    lines.push(STAGING_COLUMNS.map((c) => csvField(row[c])).join(","));
  }
  writeFileSync(outPath, lines.join("\n") + "\n");

  console.error(
    `[transform-and-score] ${files.length} result files (${unmatchedFiles} unmatched) -> ${rows.length} place rows -> ${outPath}`
  );
}

main();
