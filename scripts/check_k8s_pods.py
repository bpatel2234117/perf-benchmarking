#!/usr/bin/env python3
"""
Standalone Kubernetes connectivity + pod listing helper.

Purpose:
- verify that `kubectl` is available
- verify the current kube context is reachable
- fetch pod status from a cluster without integrating into the main app

Designed to run on macOS or Windows as long as:
- Python 3 is installed
- `kubectl` is installed and configured

Examples:
  python scripts/check_k8s_pods.py
  python scripts/check_k8s_pods.py --namespace bigid
  python scripts/check_k8s_pods.py --all-namespaces
  python scripts/check_k8s_pods.py --context my-eks-context --show-json
  python scripts/check_k8s_pods.py --namespace bigid --selector app=scanner
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _run_command(cmd: Sequence[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _kubectl_path() -> Optional[str]:
    return shutil.which("kubectl")


def _build_base_kubectl_cmd(args: argparse.Namespace) -> List[str]:
    kubectl = _kubectl_path()
    if not kubectl:
        raise RuntimeError(
            "`kubectl` was not found in PATH. Install kubectl and configure kube access first."
        )

    cmd = [kubectl]
    if args.kubeconfig:
        cmd.extend(["--kubeconfig", str(Path(args.kubeconfig))])
    if args.context:
        cmd.extend(["--context", args.context])
    return cmd


def _kubectl_json(args: argparse.Namespace, kubectl_args: Sequence[str]) -> Dict[str, Any]:
    cmd = _build_base_kubectl_cmd(args) + list(kubectl_args) + ["-o", "json"]
    rc, out, err = _run_command(cmd)
    if rc != 0:
        raise RuntimeError(err or out or f"kubectl failed with exit code {rc}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kubectl returned non-JSON output: {exc}") from exc


def _get_current_context(args: argparse.Namespace) -> str:
    cmd = _build_base_kubectl_cmd(args) + ["config", "current-context"]
    rc, out, err = _run_command(cmd)
    if rc != 0:
        raise RuntimeError(err or out or "Unable to read current kubectl context")
    return out.strip()


def _check_cluster_connection(args: argparse.Namespace) -> Dict[str, Any]:
    cmd = _build_base_kubectl_cmd(args) + ["cluster-info"]
    rc, out, err = _run_command(cmd)
    if rc != 0:
        raise RuntimeError(err or out or "Unable to reach cluster with kubectl")
    return {"ok": True, "output": out}


def _list_pods(args: argparse.Namespace) -> Dict[str, Any]:
    cmd = ["get", "pods"]
    if args.all_namespaces:
        cmd.append("--all-namespaces")
    elif args.namespace:
        cmd.extend(["--namespace", args.namespace])
    if args.selector:
        cmd.extend(["--selector", args.selector])
    return _kubectl_json(args, cmd)


def _container_readiness(item: Dict[str, Any]) -> str:
    statuses = (((item.get("status") or {}).get("containerStatuses")) or [])
    if not statuses:
        return "0/0"
    ready = sum(1 for st in statuses if bool(st.get("ready")))
    total = len(statuses)
    return f"{ready}/{total}"


def _restart_count(item: Dict[str, Any]) -> int:
    statuses = (((item.get("status") or {}).get("containerStatuses")) or [])
    total = 0
    for st in statuses:
        try:
            total += int(st.get("restartCount") or 0)
        except Exception:
            continue
    return total


def _waiting_or_terminated_reason(item: Dict[str, Any]) -> str:
    statuses = (((item.get("status") or {}).get("containerStatuses")) or [])
    reasons: List[str] = []
    for st in statuses:
        state = st.get("state") or {}
        waiting = state.get("waiting") or {}
        terminated = state.get("terminated") or {}
        reason = waiting.get("reason") or terminated.get("reason") or ""
        if reason:
            reasons.append(str(reason))
    seen = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return ", ".join(seen)


def _pod_rows(pods_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in pods_json.get("items") or []:
        metadata = item.get("metadata") or {}
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        rows.append(
            {
                "namespace": str(metadata.get("namespace") or ""),
                "name": str(metadata.get("name") or ""),
                "phase": str(status.get("phase") or ""),
                "ready": _container_readiness(item),
                "restarts": _restart_count(item),
                "node": str(spec.get("nodeName") or ""),
                "pod_ip": str(status.get("podIP") or ""),
                "reason": _waiting_or_terminated_reason(item),
            }
        )
    rows.sort(key=lambda r: (r["namespace"], r["name"]))
    return rows


def _print_summary(rows: List[Dict[str, Any]], *, context: str) -> None:
    print(f"Connected context: {context}")
    print(f"Pods returned: {len(rows)}")
    if not rows:
        return

    phase_counts: Dict[str, int] = {}
    for row in rows:
        phase = row["phase"] or "Unknown"
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
    print("Phase summary:")
    for phase, count in sorted(phase_counts.items(), key=lambda x: x[0].lower()):
        print(f"  - {phase}: {count}")


def _print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("No pods found.")
        return

    headers = ["NAMESPACE", "NAME", "PHASE", "READY", "RESTARTS", "NODE", "REASON"]
    keys = ["namespace", "name", "phase", "ready", "restarts", "node", "reason"]

    widths = []
    for header, key in zip(headers, keys):
        max_width = len(header)
        for row in rows:
            value = str(row.get(key) or "")
            if len(value) > max_width:
                max_width = len(value)
        widths.append(min(max_width, 60))

    def _clip(value: Any, width: int) -> str:
        txt = str(value or "")
        if len(txt) <= width:
            return txt
        if width <= 3:
            return txt[:width]
        return txt[: width - 3] + "..."

    header_line = "  ".join(_clip(h, w).ljust(w) for h, w in zip(headers, widths))
    print(header_line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(
            "  ".join(
                _clip(row.get(key) or "", width).ljust(width)
                for key, width in zip(keys, widths)
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Kubernetes connection and list pod statuses using kubectl."
    )
    parser.add_argument("--context", help="kubectl context name to use")
    parser.add_argument("--kubeconfig", help="optional kubeconfig path")
    parser.add_argument("--namespace", help="namespace to query")
    parser.add_argument(
        "--all-namespaces",
        action="store_true",
        help="list pods across all namespaces",
    )
    parser.add_argument("--selector", help="label selector, for example app=scanner")
    parser.add_argument(
        "--show-json",
        action="store_true",
        help="print raw pod JSON instead of the table view",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.namespace and args.all_namespaces:
        parser.error("--namespace and --all-namespaces cannot be used together")

    try:
        kubectl = _kubectl_path()
        if not kubectl:
            raise RuntimeError(
                "`kubectl` is not installed or not available in PATH."
            )

        print(f"kubectl: {kubectl}")
        context = _get_current_context(args)
        _check_cluster_connection(args)
        pods_json = _list_pods(args)
        rows = _pod_rows(pods_json)

        _print_summary(rows, context=context)
        print()
        if args.show_json:
            print(json.dumps(pods_json, indent=2))
        else:
            _print_table(rows)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
