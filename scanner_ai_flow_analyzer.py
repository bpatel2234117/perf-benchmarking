import os
import re
import zipfile
import pandas as pd
from datetime import datetime
import requests
import json

ZIP_FILE = "smb_scanner-logs-2026-03-01.zip"
EXTRACT_DIR = "scanner_logs_ai"
OUTPUT_DIR = "scanner_ai_output"

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"

# ==========================================================
# Setup
# ==========================================================

def setup():
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

def extract_zip():
    with zipfile.ZipFile(ZIP_FILE, 'r') as zip_ref:
        zip_ref.extractall(EXTRACT_DIR)

# ==========================================================
# Parse Logs
# ==========================================================

def parse_logs():
    records = []

    for root, _, files in os.walk(EXTRACT_DIR):
        for file in files:
            path = os.path.join(root, file)

            with open(path, errors="ignore") as f:
                for line in f:
                    ts_match = re.search(
                        r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\.\d+Z',
                        line
                    )
                    if not ts_match:
                        continue

                    ts_str = ts_match.group().replace("T", " ")
                    timestamp = datetime.strptime(
                        ts_str,
                        "%Y-%m-%d %H:%M:%S.%fZ"
                    )

                    level_match = re.search(r'\[(INFO|ERROR|WARN|DEBUG)\]', line)
                    level = level_match.group(1) if level_match else "UNKNOWN"

                    records.append({
                        "timestamp": timestamp,
                        "level": level,
                        "message": line.strip()
                    })

    df = pd.DataFrame(records)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    return df

# ==========================================================
# Domain Extraction
# ==========================================================

def detect_processing(df):
    patterns = [
        "subscan",
        "enumeration",
        "classification",
        "processing",
        "scanned"
    ]

    return df[
        df["message"].str.contains("|".join(patterns), case=False, na=False)
    ]

def detect_completion(df):
    completion_patterns = [
        "scan completed",
        "marking scan",
        "parent scan completed",
        "finished successfully"
    ]

    return df[
        df["message"].str.contains("|".join(completion_patterns), case=False, na=False)
    ]

def detect_scanner_job_failures(df):
    return df[
        df["message"].str.contains("scanner_jobs", case=False, na=False) &
        df["message"].str.contains("error|empty|timeout|missing", case=False, na=False)
    ]

def detect_polling(df):
    return df[
        df["message"].str.contains("scanner_jobs", case=False, na=False)
    ]

# ==========================================================
# Build Structured Evidence
# ==========================================================

def build_evidence(df):

    processing = detect_processing(df)
    completion = detect_completion(df)
    job_failures = detect_scanner_job_failures(df)
    polling = detect_polling(df)

    evidence = {}

    evidence["log_start"] = str(df["timestamp"].min())
    evidence["log_end"] = str(df["timestamp"].max())
    evidence["total_log_lines"] = len(df)

    evidence["last_processing_event"] = (
        str(processing["timestamp"].max()) if not processing.empty else None
    )

    evidence["first_scanner_job_failure"] = (
        str(job_failures["timestamp"].min()) if not job_failures.empty else None
    )

    evidence["failure_count"] = len(job_failures)

    evidence["failure_sample"] = (
        job_failures.iloc[0]["message"] if not job_failures.empty else None
    )

    evidence["completion_detected"] = not completion.empty

    evidence["completion_time"] = (
        str(completion["timestamp"].max()) if not completion.empty else None
    )

    if evidence["last_processing_event"] and evidence["first_scanner_job_failure"]:
        delta = (
            pd.to_datetime(evidence["last_processing_event"]) -
            pd.to_datetime(evidence["first_scanner_job_failure"])
        ).total_seconds()

        evidence["seconds_between_failure_and_processing_stop"] = delta

    if evidence["last_processing_event"]:
        last_proc_time = pd.to_datetime(evidence["last_processing_event"])
        polling_after = polling[polling["timestamp"] > last_proc_time]
        evidence["polling_after_processing_stop"] = len(polling_after)

    return evidence

# ==========================================================
# LLM Prompt Builder
# ==========================================================

def build_prompt(evidence):

    return f"""
You are an expert distributed systems forensic analyst.

Below are structured facts extracted from scanner logs.

Facts:
- Log Start: {evidence.get("log_start")}
- Log End: {evidence.get("log_end")}
- Last Processing Event: {evidence.get("last_processing_event")}
- First scanner_jobs Failure: {evidence.get("first_scanner_job_failure")}
- Failure Count: {evidence.get("failure_count")}
- Completion Detected: {evidence.get("completion_detected")}
- Completion Time: {evidence.get("completion_time")}
- Polling After Processing Stop: {evidence.get("polling_after_processing_stop")}
- Seconds Between Failure and Stop: {evidence.get("seconds_between_failure_and_processing_stop")}
- Sample Failure Message: {evidence.get("failure_sample")}

Tasks:
1. Reconstruct the execution flow.
2. Explain what happened.
3. Identify whether scanner or orchestration is responsible.
4. Explain why polling continued.
5. Determine if this is logical deadlock, crash, or protocol violation.
6. Provide final root cause conclusion.

Provide structured reasoning.
Do not repeat the facts blindly — interpret them.
"""

# ==========================================================
# Call Ollama LLM
# ==========================================================

def call_llm(prompt):

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False
        }
    )

    return response.json()["response"]

# ==========================================================
# MAIN
# ==========================================================

def main():

    print("Setting up...")
    setup()

    print("Extracting logs...")
    extract_zip()

    print("Parsing logs...")
    df = parse_logs()

    print("Building structured evidence...")
    evidence = build_evidence(df)

    print("Generating AI reasoning...")
    prompt = build_prompt(evidence)
    ai_explanation = call_llm(prompt)

    print("\n===== AI ROOT CAUSE EXPLANATION =====\n")
    print(ai_explanation)

    with open(f"{OUTPUT_DIR}/ai_root_cause.txt", "w") as f:
        f.write(ai_explanation)

if __name__ == "__main__":
    main()