#!/usr/bin/env python3
"""Batch Maps scraper client.

Submits keywords in parallel (up to 20 at a time), polls for results,
and saves each completed job to a JSON file.

Usage:
    # Keywords as arguments
    python scrape.py --base-url https://example.com --api-key gms_... "cafes in athens" "hotels in berlin"

    # Keywords from stdin (one per line)
    cat keywords.txt | python scrape.py --base-url https://example.com --api-key gms_...

    # Custom output directory
    python scrape.py --base-url https://example.com --api-key gms_... -o results "cafes in athens"

    # Skip TLS certificate verification (e.g. self-signed certs)
    python scrape.py --base-url https://example.com --api-key gms_... --insecure "cafes in athens"
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def safe_filename(s: str) -> str:
    """Convert a string to a safe filename component."""
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s]+", "_", s)
    return s[:100]


def api_request(base_url: str, api_key: str, method: str, path: str, body=None,
                ssl_ctx=None):
    """Make an API request and return parsed JSON."""
    url = base_url.rstrip("/") + path
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)

    resp = urlopen(req, timeout=30, context=ssl_ctx)
    return json.loads(resp.read())


def submit_job(base_url: str, api_key: str, keyword: str, lang: str = "en",
               max_depth: int = 1, ssl_ctx=None) -> dict:
    """Submit a scrape job and return {"job_id": ..., "keyword": ...}."""
    resp = api_request(base_url, api_key, "POST", "/api/v1/scrape", {
        "keyword": keyword,
        "lang": lang,
        "max_depth": max_depth,
    }, ssl_ctx=ssl_ctx)
    return {"job_id": resp["job_id"], "keyword": keyword}


def poll_job(base_url: str, api_key: str, job_id: str, keyword: str,
             output_dir: str, jobids_dir: str = None, poll_interval: float = 5.0,
             poll_timeout: float = 600.0, ssl_ctx=None):
    """Poll a job until completion. Returns True on success, False on a
    terminal 'failed' status or if the job outlives poll_timeout seconds.
    Raises on transport/HTTP errors so the caller can decide whether to retry.

    The timeout exists because a job wedged server-side stays in 'running'
    forever -- without it a single stuck job parks a worker thread for the
    rest of the batch."""
    deadline = time.monotonic() + poll_timeout
    while True:
        resp = api_request(base_url, api_key, "GET", f"/api/v1/jobs/{job_id}",
                           ssl_ctx=ssl_ctx)
        status = resp.get("status", "")

        if status == "completed":
            fname = f"{job_id}-{safe_filename(keyword)}.json"
            path = os.path.join(output_dir, fname)
            with open(path, "w") as f:
                json.dump(resp.get("results", []), f, indent=2)
            count = resp.get("result_count", 0)
            print(f"  [done] {keyword!r} -> {count} results -> {fname}")

            # Create symlink in jobids directory if provided
            if jobids_dir:
                os.makedirs(jobids_dir, exist_ok=True)
                jobid_link = os.path.join(jobids_dir, job_id)
                if not os.path.exists(jobid_link):
                    os.symlink(path, jobid_link)
            return True

        if status == "failed":
            err = resp.get("error", "unknown error")
            print(f"  [fail] {keyword!r}: {err}", file=sys.stderr)
            return False

        if time.monotonic() >= deadline:
            print(f"  [timeout] {keyword!r}: job {job_id} still {status!r} after "
                  f"{poll_timeout:.0f}s, abandoning", file=sys.stderr)
            return False

        time.sleep(poll_interval)


def process_keyword(base_url: str, api_key: str, keyword: str, output_dir: str,
                    jobids_dir: str = None, lang: str = "en", max_depth: int = 1, ssl_ctx=None,
                    max_retries: int = 3, retry_delay: float = 15.0,
                    poll_timeout: float = 600.0):
    """Submit a keyword, poll until done, save results. Any failure -- a
    submit error, a transport error while polling, a terminal 'failed'
    job status, or a job that outlives poll_timeout -- reschedules the
    keyword (resubmits as a brand new job) with linear backoff, up to
    max_retries times."""
    attempt = 0
    while True:
        attempt += 1
        try:
            job = submit_job(base_url, api_key, keyword, lang=lang, max_depth=max_depth,
                             ssl_ctx=ssl_ctx)
            print(f"  [submitted] {keyword!r} -> job {job['job_id']} (attempt {attempt}/{max_retries + 1})")
            ok = poll_job(base_url, api_key, job["job_id"], keyword, output_dir,
                         jobids_dir=jobids_dir, poll_timeout=poll_timeout,
                         ssl_ctx=ssl_ctx)
            if ok:
                return
        except HTTPError as e:
            body = e.read().decode()
            print(f"  [error] {keyword!r} (attempt {attempt}/{max_retries + 1}): HTTP {e.code} {body}", file=sys.stderr)
        except Exception as e:
            print(f"  [error] {keyword!r} (attempt {attempt}/{max_retries + 1}): {e}", file=sys.stderr)

        if attempt > max_retries:
            print(f"  [gave up] {keyword!r} after {attempt} attempt(s)", file=sys.stderr)
            return

        wait = retry_delay * attempt
        print(f"  [reschedule] {keyword!r} retrying in {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(
        description="Batch Maps scraper client",
    )
    parser.add_argument("--base-url", default=os.environ.get('BASE_URL'),help="API base URL")
    parser.add_argument("--api-key", default=os.environ.get('API_KEY'), help="API key")
    parser.add_argument("-o", "--output", default="map-outputs",
                        help="Output directory (default: map-outputs)")
    parser.add_argument("--jobids-dir", default=None,
                        help="Directory for jobid symlinks (optional)")
    parser.add_argument("-w", "--workers", type=int, default=20,
                        help="Max parallel jobs (default: 20)")
    parser.add_argument("--lang", default="en",
                        help="Language for results (default: en)")
    parser.add_argument("--max-depth", type=int, default=1,
                        help="Max scrape depth (default: 1)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Reschedule a failed job up to this many times (default: 3)")
    parser.add_argument("--retry-delay", type=float, default=15.0,
                        help="Base delay in seconds before rescheduling a failed job, scaled by attempt number (default: 15)")
    parser.add_argument("--poll-timeout", type=float, default=600.0,
                        help="Give up polling a single job after this many seconds and reschedule it (default: 600)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Skip TLS certificate verification (e.g. for self-signed certs)")
    parser.add_argument("keywords", nargs="*",
                        help="Keywords to scrape (reads from stdin if none given)")

    args = parser.parse_args()

    ssl_ctx = None
    if args.insecure:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    keywords = args.keywords
    if not keywords:
        if sys.stdin.isatty():
            parser.error("provide keywords as arguments or pipe them via stdin")
        keywords = [line.strip() for line in sys.stdin if line.strip()]

    if not keywords:
        parser.error("no keywords provided")

    os.makedirs(args.output, exist_ok=True)

    print(f"Scraping {len(keywords)} keyword(s), max {args.workers} parallel, output -> {args.output}/")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_keyword, args.base_url, args.api_key, kw, args.output,
                        args.jobids_dir, args.lang, args.max_depth, ssl_ctx,
                        args.max_retries, args.retry_delay, args.poll_timeout): kw
            for kw in keywords
        }
        for future in as_completed(futures):
            exc = future.exception()
            if exc:
                kw = futures[future]
                print(f"  [error] {kw!r}: {exc}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
