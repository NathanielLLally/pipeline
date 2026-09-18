# Phase 1 — dog_training Pipeline Execution Plan
# 16 metros × 3 workers × 600s poll timeout
#
# Status:
#   ✓ Los Angeles, CA — COMPLETED (11 geo-targets, 263 businesses)
#   ✓ New York, NY — PARTIAL (9 of 11 geo-targets, 210 businesses, 33 query gap due to rate-limit)
#   ◌ Chicago, IL — PENDING
#   ◌ Houston, TX — PENDING
#   ◌ Phoenix, AZ — PENDING
#   ◌ Dallas, TX — PENDING
#   ◌ San Francisco Bay Area, CA — PENDING
#   ◌ Seattle, WA — PENDING
#   ◌ Denver, CO — PENDING
#   ◌ Boston, MA — PENDING
#   ◌ Miami, FL — PENDING
#   ◌ Atlanta, GA — PENDING
#   ◌ Washington, DC — PENDING
#   ◌ Austin, TX — PENDING
#   ◌ Philadelphia, PA — PENDING
#   ◌ San Diego, CA — PENDING

## COMPLETED
#cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Los Angeles, CA" --workers 3
#cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "New York, NY" --workers 3

## PENDING (in priority order)
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Chicago, IL" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Houston, TX" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Phoenix, AZ" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Dallas, TX" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "San Francisco Bay Area, CA" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Seattle, WA" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Denver, CO" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Boston, MA" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Miami, FL" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Atlanta, GA" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Washington, DC" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Austin, TX" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Philadelphia, PA" --workers 3
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry && set -a && source .env && set +a && ./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "San Diego, CA" --workers 3
