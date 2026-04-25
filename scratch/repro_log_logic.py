import re
import os

def _skip_info_level_line(line: str) -> bool:
    if os.getenv("SCANNER_LOG_SKIP_INFO_LEVEL", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return bool(re.search(r"\[(?:INFO|DEBUG|TRACE)\]", line, re.I))

def _line_for_error_keyword_scan(line: str) -> str:
    return re.sub(r"https?://[^\s]+", " ", line, flags=re.I)

def _line_passes_error_level_gate(line: str) -> bool:
    mode = os.getenv("SCANNER_LOG_ERROR_REQUIRE_LEVEL", "error_fatal").strip().lower()
    if mode in ("any", "legacy", "all", "0", "off", "false", "no"):
        return True
    if mode in ("warn", "warning"):
        return bool(re.search(r"\[(?:ERROR|FATAL|SEVERE|WARN|WARNING)\]", line, re.I))
    return bool(re.search(r"\[(?:ERROR|FATAL|SEVERE)\]", line, re.I))

def detect_errors_in_logs(log_text):
    lines = log_text.splitlines()
    error_contexts = []
    seen_fingerprints = set()

    i = 0
    ERROR_PATTERN = re.compile(
        r"(?i)(?:"
        r"\berror\b|\bfatal\b|\bfail\b|\bfailed\b(?=\s)|\bfailure\b|\bsevere\b|\bexception\b|\baxioserror\b|\bnohttp\b|"
        r"status\s+[45]\d{2}|\btimeout\b|timed?\s*out|\brefused\b|\bunreachable\b|\breset\b|\bdenied\b|"
        r"\babort(?:ed|ing)?\b|\bexhausted\b|"
        r"\bthrottl(?:ing|ed)\b|\blimitreached\b|\bgraphservice\b|\bactivitylimit\b"
        r")"
    )

    while i < len(lines):
        line = lines[i]
        if _skip_info_level_line(line):
            i += 1
            continue
        scan_line = _line_for_error_keyword_scan(line)
        if ERROR_PATTERN.search(scan_line):
            if not _line_passes_error_level_gate(line):
                i += 1
                continue
            start = max(0, i - 2)
            end = i + 1
            while end < len(lines) and (end - i) < 30:
                next_line = lines[end]
                if next_line.startswith((" ", "\t", "at ", "Caused by:", "---", "}", "]")):
                    end += 1
                else: break
            if end == i + 1:
                end = min(len(lines), i + 4)
            ctx_text = "\n".join(lines[start:end])
            fingerprint = ctx_text[:300]
            if fingerprint not in seen_fingerprints:
                seen_fingerprints.add(fingerprint)
                error_contexts.append(ctx_text)
            i = end
        else:
            i += 1
    return error_contexts

log_data = """
2026-04-19 15:02:02.963Z] [ERROR] default -  got error while fetching configs Error: socket hang up
    at Socket.socketCloseListener (node:_http_client:548:27)
    at Socket.emit (node:events:531:35)
    at Socket.emit (node:domain:489:12)
    at TCP.<anonymous> (node:net:346:12)
    at TCP.callbackTrampoline (node:internal/async_hooks:130:17)

[2026-04-19 15:02:11.670Z] [INFO] default - ::ffff:10.57.97.138 - - "GET /health/readiness HTTP/1.1" 200 77 "" "kube-probe/1.34+" 8

2026-04-19 15:02:10.050Z] [ERROR] default - [fileName: queue-threshold.js] [funcName: isQueueThresholdReached] [processDesc: pii_findings_bulk_queue_job] [traceId: 802c8422-be3b-47a4-9ab9-9ff504ada090] [spanId: 0199e9bb-db5b-4a02-8585-ee93b647e56c] [parentSpanId: 4051e929-ceb4-4063-86f6-161143547b51] failed to check queues overload AxiosError: Request failed with status code 404
    at settle (/usr/src/app/node_modules/axios/dist/node/axios.cjs:2151:12)
    at IncomingMessage.handleStreamEnd (/usr/src/app/node_modules/axios/dist/node/axios.cjs:3545:11)
"""

res = detect_errors_in_logs(log_data)
print(f"Detected {len(res)} errors")
for i, ctx in enumerate(res):
    print(f"--- Error {i+1} ---")
    print(ctx)
