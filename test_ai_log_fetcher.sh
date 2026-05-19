#!/bin/bash
cd v2_api
python3 ai_log_fetcher.py 2>&1 | head -300 &
PID=$!
sleep 50
kill $PID 2>/dev/null
wait $PID 2>/dev/null
