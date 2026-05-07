#!/usr/bin/env bash

# Kill any existing stray processes on these ports first
echo "[-] Cleaning up ports 8000 and 5174..."
lsof -ti:8000 | xargs kill -9 2>/dev/null || true
lsof -ti:5174 | xargs kill -9 2>/dev/null || true

# Kill existing workflow_runner and tracker processes
echo "[-] Stopping existing v2_api workflow_runner and tracker..."
pkill -f "python.*v2_api/workflow_runner.py" 2>/dev/null || true
pkill -f "python.*v2_api/tracker.py" 2>/dev/null || true
pkill -f "python.*v2_api/ai_log_fetcher.py" 2>/dev/null || true

echo "========================================="
echo " Starting BigID Benchmarks V2 Full Stack"
echo "========================================="

# Start the Backend
echo "[+] Starting FastAPI Backend (v2_api)..."
cd v2_api
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!
cd ..

# Start the Workflow Runner (Postgres)
echo "[+] Starting V2 Workflow Runner (Postgres)..."
python3 v2_api/workflow_runner.py --interval-seconds 30 > logs/v2_workflow_runner.log 2>&1 &
WORKFLOW_PID=$!

# Start the Tracker (Postgres)
echo "[+] Starting V2 Tracker (Postgres)..."
python3 v2_api/tracker.py --interval-seconds 60 > logs/v2_tracker.log 2>&1 &
TRACKER_PID=$!

# Start the AI Log Fetcher (Postgres)
echo "[+] Starting V2 AI Log Fetcher (Postgres)..."
python3 v2_api/ai_log_fetcher.py > logs/v2_ai_log_fetcher.log 2>&1 &
AI_LOG_PID=$!

# Wait a moment for backend to start
sleep 2

# Start the Frontend
echo "[+] Starting React Frontend (v2_frontend)..."
cd v2_frontend
npm run dev -- --host 127.0.0.1 --port 5174 --strictPort &
FRONTEND_PID=$!
cd ..

echo "========================================="
echo " All V2 Services are running!"
echo " Frontend:        http://127.0.0.1:5174"
echo " Backend API:     http://127.0.0.1:8000"
echo " Workflow Runner: PID $WORKFLOW_PID"
echo " Tracker:         PID $TRACKER_PID"
echo " AI Log Fetcher:  PID $AI_LOG_PID"
echo ""
echo " Logs:"
echo "   Workflow: logs/v2_workflow_runner.log"
echo "   Tracker:  logs/v2_tracker.log"
echo "   AI Logs:  logs/v2_ai_log_fetcher.log"
echo ""
echo " Press Ctrl+C to stop all services."
echo "========================================="

# Trap Ctrl+C to gracefully shut down the background processes
cleanup() {
    echo ""
    echo "[!] Shutting down V2 Full Stack..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    kill $WORKFLOW_PID 2>/dev/null || true
    kill $TRACKER_PID 2>/dev/null || true
    kill $AI_LOG_PID 2>/dev/null || true
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# Keep script running to maintain the trap
wait $BACKEND_PID $FRONTEND_PID $WORKFLOW_PID $TRACKER_PID $AI_LOG_PID
