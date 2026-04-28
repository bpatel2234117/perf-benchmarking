#!/usr/bin/env bash

# Kill any existing stray processes on these ports first
echo "[-] Cleaning up ports 8000 and 5174..."
lsof -ti:8000 | xargs kill -9 2>/dev/null || true
lsof -ti:5174 | xargs kill -9 2>/dev/null || true

echo "========================================="
echo " Starting BigID Benchmarks V2 Stack      "
echo "========================================="

# Start the Backend
echo "[+] Starting FastAPI Backend (v2_api)..."
cd v2_api
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!
cd ..

# Start the Frontend
echo "[+] Starting React Frontend (v2_frontend)..."
cd v2_frontend
npm run dev -- --host 127.0.0.1 --port 5174 --strictPort &
FRONTEND_PID=$!
cd ..

echo "========================================="
echo " Services are running!"
echo " Frontend: http://127.0.0.1:5174"
echo " Backend:  http://127.0.0.1:8000"
echo " Press Ctrl+C to stop both services."
echo "========================================="

# Trap Ctrl+C to gracefully shut down the background processes
cleanup() {
    echo ""
    echo "[!] Shutting down V2 Stack..."
    kill $BACKEND_PID
    kill $FRONTEND_PID
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# Keep script running to maintain the trap
wait $BACKEND_PID $FRONTEND_PID
