from __future__ import annotations

import json
import subprocess as sp
import time


WORKLOAD_URL = "http://172.16.0.2:8080/cli-pipeline"

# "health-daemon",                  
# "health-exec",                      
# "python-json-tool",            
# "cli-pipeline",                    
# "random-rg-scan-fixed",    
# "random-rg-scan-random",  
# "read-list",                          
# "agent-tool-replay",          

def run_workload(item, timeout):
    i, n = item
    start_ns = time.monotonic_ns()
    r = sp.run(
        ["ip", "netns", "exec", n["ns"], "curl", "-i", WORKLOAD_URL],
        text=True,
        stdout=sp.PIPE,
        stderr=sp.PIPE,
        timeout=timeout + 5,
    )
    end_ns = time.monotonic_ns()
    if r.returncode:
        raise RuntimeError(f"sandbox {i}: {r.stderr.strip() or r.stdout.strip()}")
    agent = parse_curl_output(r.stdout)
    if not (200 <= int(agent.get("http_status", 0)) < 300):
        raise RuntimeError(f"sandbox {i}: HTTP {agent.get('http_status')}: {agent.get('raw_body', '').strip()}")
    if agent.get("ok") is False:
        raise RuntimeError(f"sandbox {i}: workload error: {agent.get('stderr') or agent.get('raw_body', '')}".strip())
    if agent.get("status") == "error":
        raise RuntimeError(f"sandbox {i}: workload error: {agent.get('detail', agent.get('raw_body', '')).strip()}")
    agent["request_start_ns"] = start_ns
    agent["request_end_ns"] = end_ns
    agent["client_round_trip_ms"] = (end_ns - start_ns) / 1_000_000
    return simplify_result(i, agent)


def parse_curl_output(output):
    headers, _, body = output.replace("\r\n", "\n").rpartition("\n\n")
    body = body.strip()
    status = 0
    for line in headers.splitlines():
        if line.startswith("HTTP/"):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                try:
                    status = int(parts[1])
                except ValueError:
                    pass
    if not body:
        return {"http_status": status}
    try:
        result = json.loads(body)
        result["http_status"] = status
        return result
    except json.JSONDecodeError:
        pass

    result = {"http_status": status, "raw_body": body}
    for line in body.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


def simplify_result(sandbox_id, agent):
    inner = {}
    for line in reversed(str(agent.get("stdout", "")).splitlines()):
        try:
            inner = json.loads(line)
            break
        except json.JSONDecodeError:
            pass
    elapsed_ns = agent.get("elapsed_ns")
    try:
        elapsed_ms = float(elapsed_ns) / 1_000_000 if elapsed_ns is not None else None
    except (TypeError, ValueError):
        elapsed_ms = None
    simple = {
        "sandbox": sandbox_id,
        "workload_ms": inner.get("duration_ms") or agent.get("duration_ms") or elapsed_ms,
        "guest_process_ms": agent.get("duration_ms") or elapsed_ms,
        "client_round_trip_ms": agent.get("client_round_trip_ms"),
    }
    if "status" in agent:
        simple["status"] = agent["status"]
    if "workload" in agent:
        simple["workload"] = agent["workload"]
    if "minflt" in agent:
        simple["minflt"] = agent["minflt"]
    if "majflt" in agent:
        simple["majflt"] = agent["majflt"]
    if "detail" in agent:
        simple["detail"] = agent["detail"]
    return simple, agent.get("request_start_ns"), agent.get("request_end_ns")
