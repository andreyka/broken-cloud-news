#!/bin/bash
set -e
cd ~/broken_cloud_news
LOG=/tmp/bcn_daily_$(date +%Y%m%d).log

echo "[$(date)] Starting daily briefing pipeline" >> $LOG

echo "[$(date)] Step 1: Collect" >> $LOG
docker compose exec -T bcn bcn collect >> $LOG 2>&1

echo "[$(date)] Step 2: Analyze" >> $LOG
docker compose exec -T bcn bcn analyze >> $LOG 2>&1

echo "[$(date)] Step 3: Write" >> $LOG
docker compose exec -T bcn bcn write >> $LOG 2>&1

echo "[$(date)] Step 4: Distribute" >> $LOG
docker compose exec -T bcn bcn distribute >> $LOG 2>&1

echo "[$(date)] Pipeline complete" >> $LOG
