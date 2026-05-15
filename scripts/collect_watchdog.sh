#!/usr/bin/env bash
# collect_watchdog.sh — keep auto-collect alive forever:
#   * loop auto-collect campaigns
#   * after each campaign, ping LCU
#   * if LCU dead, try restart via Riot Client; if still dead after CLIENT_TIMEOUT_SEC, abort
#   * stop sentinel: touch data/lcu/STOP_WATCHDOG (any time) → exit at next iteration boundary
#
# Args (env vars):
#   ROUNDS_PER_CAMPAIGN  (default 10)
#   TARGET_GAMES         (default 500)
#   MAX_PLAYERS          (default 1000)
#   PAGES_PER_ROUND      (default 2)
#   RC_EXE               (default detected from RiotClientInstalls.json)
#   CLIENT_TIMEOUT_SEC   (default 1800 = 30 min)
#
# Logs: data/lcu/watchdog.log + per-campaign data/lcu/auto_collect_wd_<n>.log
set -u

ROUNDS_PER_CAMPAIGN="${ROUNDS_PER_CAMPAIGN:-10}"
TARGET_GAMES="${TARGET_GAMES:-500}"
MAX_PLAYERS="${MAX_PLAYERS:-1000}"
PAGES_PER_ROUND="${PAGES_PER_ROUND:-2}"
CLIENT_TIMEOUT_SEC="${CLIENT_TIMEOUT_SEC:-1800}"
RC_EXE="${RC_EXE:-D:/遊戲/Riot Games/Riot Client/RiotClientServices.exe}"
STOP_FILE="data/lcu/STOP_WATCHDOG"
LOG="data/lcu/watchdog.log"
mkdir -p data/lcu

log() {
  echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*" | tee -a "$LOG"
}

probe_lcu() {
  PYTHONIOENCODING=utf-8 python -X utf8 -c "
from aram_nn.lcu.process import get_credentials
from aram_nn.lcu.client import LCUClient, get_current_summoner
c = get_credentials()
if not c: import sys; sys.exit(1)
s = get_current_summoner(LCUClient(c))
if not s or not s.get('puuid'): import sys; sys.exit(1)
" 2>/dev/null
}

restart_client() {
  log "client dead — launching: $RC_EXE"
  PYTHONIOENCODING=utf-8 python -X utf8 -c "
import subprocess, sys
subprocess.Popen(['$RC_EXE', '--launch-product=league_of_legends', '--launch-patchline=live'])
" >/dev/null 2>&1 || log "WARN: could not spawn Riot Client launcher"
  local waited=0
  while [ "$waited" -lt "$CLIENT_TIMEOUT_SEC" ]; do
    if probe_lcu; then
      log "LCU back up after ${waited}s"
      return 0
    fi
    [ -f "$STOP_FILE" ] && { log "STOP_WATCHDOG appeared during client wait — aborting"; return 1; }
    sleep 30
    waited=$((waited + 30))
  done
  log "LCU did not come back within ${CLIENT_TIMEOUT_SEC}s — giving up"
  return 1
}

iter=1
log "watchdog starting — rounds_per_campaign=$ROUNDS_PER_CAMPAIGN target_games=$TARGET_GAMES max_players=$MAX_PLAYERS pages=$PAGES_PER_ROUND client_timeout=${CLIENT_TIMEOUT_SEC}s"

while true; do
  if [ -f "$STOP_FILE" ]; then
    log "STOP_WATCHDOG sentinel detected — exiting"
    rm -f "$STOP_FILE"
    exit 0
  fi
  # 1. Ensure LCU alive before launching campaign
  if ! probe_lcu; then
    if ! restart_client; then
      exit 2
    fi
  fi
  # 2. Launch one auto-collect campaign
  campaign_log="data/lcu/auto_collect_wd_${iter}.log"
  campaign_err="data/lcu/auto_collect_wd_${iter}.err.log"
  log "campaign #${iter} starting → $campaign_log"
  PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 python -u -X utf8 \
    scripts/lcu_collector.py auto-collect \
    --rounds "$ROUNDS_PER_CAMPAIGN" \
    --target-games "$TARGET_GAMES" \
    --max-players "$MAX_PLAYERS" \
    --games-per-player 4 \
    --opgg-tier platinum --opgg-tier gold \
    --opgg-pages-per-round "$PAGES_PER_ROUND" \
    --rate-window-sec 180 --rate-min-saves 60 \
    > "$campaign_log" 2> "$campaign_err"
  rc=$?
  log "campaign #${iter} ended rc=$rc"
  iter=$((iter + 1))
  # Brief breather between campaigns
  sleep 5
done
