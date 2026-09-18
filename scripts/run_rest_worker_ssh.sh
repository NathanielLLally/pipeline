#!/bin/bash
set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOP"

# Load environment
set -a && source .env && set +a

# Session name for tmux
SESSION_NAME="scraper-workers"

# Track if we created a new session
CREATED_SESSION=0

# Check if we're already in a tmux session
if [ -z "${TMUX:-}" ]; then
  # Not in tmux, create a new session
  echo "Creating new tmux session: $SESSION_NAME"
  tmux new-session -d -s "$SESSION_NAME"
  CREATED_SESSION=1
else
  # Already in tmux, use current session name
  SESSION_NAME="$(tmux display-message -p '#{session_name}')"
  echo "Using existing tmux session: $SESSION_NAME"
fi

# Loop through hosts and open each in a tmux window
for i in "${!SCRAPER_SSH_HOSTS[@]}"; do
  host="${SCRAPER_SSH_HOSTS[$i]}"

  if [ "$CREATED_SESSION" -eq 1 ] && [ "$i" -eq 0 ]; then
    # First host in new session: rename the default window
    tmux rename-window -t "$SESSION_NAME:0" "worker-$host"
    TMUX_WINDOW="$SESSION_NAME:0"
  else
    # Create new window for subsequent hosts or if session already existed
    tmux new-window -t "$SESSION_NAME" -n "worker-$host"
    TMUX_WINDOW="$SESSION_NAME:$(tmux list-windows -t "$SESSION_NAME" | tail -1 | cut -d':' -f1)"
  fi

  # Send SSH command to window
  tmux send-keys -t "$TMUX_WINDOW" "ssh -p $SCRAPER_SSH_PORT $SCRAPER_SSH_USER@$host" "Enter"
done

# Only attach if we created a new session
if [ "$CREATED_SESSION" -eq 1 ]; then
  tmux attach-session -t "$SESSION_NAME"
else
  echo "Windows created in existing session: $SESSION_NAME"
fi
