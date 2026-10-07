# Suggested Commands

## Python Workflow

```bash
# Setup
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# Run tests
pytest                          # all tests
pytest flow/test_analysis_agent.py -v   # specific test file
pytest -k "analysis" --tb=short          # by keyword

# Run agents locally
python -m flow.agents.analysis
python -m flow.agents.research
python -m flow.agents.drafting

# Serve Prefect deployments
python flow/serve_agents.py

# Git
gh issue create --title "Bug: ..." --body "Description"  # file issues for bugs/TODOs
```

## JavaScript (Node.js)

```bash
# Run enrichment scripts
node scripts/gen-queries.mjs
node scripts/enrich-from-raw.mjs
node scripts/transform-and-score.mjs
node scripts/qc-pass.mjs

# Run tests
node scripts/lib/ads-transparency.test.mjs
node scripts/lib/emails.test.mjs
node scripts/lib/rdap.test.mjs
```

## Database

```bash
# Apply schema
psql <connection-string> < db/schema.sql

# Run queries
psql <connection-string> < db/upsert.sql
```

## Deployment

```bash
# Install watchdog systemd timer
./deploy/install-watchdog.sh              # install & start
./deploy/install-watchdog.sh --dry-run    # preview
./deploy/install-watchdog.sh --uninstall  # remove

# System utils
./refresh-proxies.sh
```

## Configuration

```bash
# Check/set environment
echo $LLM_MODEL
echo $ANALYSIS_MODEL
source .env && env | grep -i litellm
```
