"""Real Codex app-server notification/cache probe. No chat/model/Blender actions.

Uses process-only configuration overrides; no user/plugin configuration writes.
The test server selects Properties via a fixed fixture signal after initial list.
"""
from __future__ import annotations
import json
import argparse
import hashlib
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime/dynamic_domains/codex_notification"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True, help="An existing local thread; never creates a chat or starts a turn")
    thread_id = parser.parse_args().thread_id
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for name in ("trigger", "fixture_state.json"):
        (RUNTIME / name).unlink(missing_ok=True)  # exact test-owned files only
    cli = os.environ["CODEX_CLI_PATH"]
    args = [cli,
        "-c", 'plugins."blender-mcp@blender-local".enabled=false',
        "-c", "mcp_servers.blender_domain_probe=" + '{command=' + json.dumps(str(ROOT / ".venv/Scripts/python.exe")) +
               ',args=[' + json.dumps(str(ROOT / "tests/codex_domain_notification_fixture.py")) +
               '],startup_timeout_sec=20,tool_timeout_sec=20}',
        "app-server", "--stdio"]
    messages = queue.Queue()
    seq = 0
    observed = []
    with (RUNTIME / "codex.stderr.log").open("w", encoding="utf-8") as errors:
        proc = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errors, text=True, encoding="utf-8")
        def reader():
            for line in proc.stdout:
                try:
                    messages.put(json.loads(line))
                except json.JSONDecodeError:
                    pass
            messages.put({"closed": True})
        threading.Thread(target=reader, daemon=True).start()
        def request(method, params):
            nonlocal seq
            seq += 1
            proc.stdin.write(json.dumps({"id": seq, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            end = time.monotonic() + 35
            while time.monotonic() < end:
                entry = messages.get(timeout=max(.1, end - time.monotonic()))
                if entry.get("closed"):
                    raise RuntimeError("Codex closed; inspect stderr")
                if entry.get("id") == seq:
                    if "error" in entry:
                        raise RuntimeError(entry["error"])
                    return entry["result"]
                observed.append({"method": entry.get("method")})
            raise TimeoutError(method)
        try:
            request("initialize", {"clientInfo": {"name": "blender-domain-notification-probe", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
            proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
            proc.stdin.flush()
            stored = request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
            rollout = Path(stored["path"])
            before_hash = hashlib.sha256(rollout.read_bytes()).hexdigest()
            before_mtime = rollout.stat().st_mtime_ns
            try:
                request("thread/resume", {"threadId": thread_id})
            except RuntimeError as exc:
                report = {"probe_completed": False, "codex_live_refresh_verified": False,
                          "limitation": "existing_thread_has_active_writer", "error": str(exc),
                          "no_chat_created": True, "no_model_turn_started": True, "blender_calls": 0,
                          "existing_thread_rollout_unchanged": before_hash == hashlib.sha256(rollout.read_bytes()).hexdigest(),
                          "existing_thread_mtime_unchanged": before_mtime == rollout.stat().st_mtime_ns,
                          "fallback": "set initial profile, sync installed plugin, reconnect MCP; do not restart Blender"}
                (RUNTIME / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False))
                return 2
            status_params = {"serverName": "blender_domain_probe", "detail": "full", "threadId": thread_id}
            before = request("mcpServerStatus/list", status_params)
            initial = next(s for s in before["data"] if s["name"] == "blender_domain_probe")
            assert len(initial["tools"]) == 25
            assert initial["serverCapabilities"]["tools"]["listChanged"] is True
            ready = json.loads((RUNTIME / "fixture_state.json").read_text(encoding="utf-8"))
            changed = request("mcpServer/tool/call", {"threadId": thread_id, "server": "blender_domain_probe",
                              "tool": "blender_set_tool_domains", "arguments": {"mode": "replace", "domains": ["properties"]}})
            (RUNTIME / "control_response.json").write_text(json.dumps(changed, indent=2), encoding="utf-8")
            # The handle must still be live; wait for server-side revision and
            # client re-listing without issuing another status request first.
            limit = time.monotonic() + 8
            automatic = None
            state = ready
            while time.monotonic() < limit:
                assert proc.poll() is None
                state = json.loads((RUNTIME / "fixture_state.json").read_text(encoding="utf-8"))
                if any(q["revision"] == 1 for q in state["list_queries"]):
                    automatic = True
                    break
                time.sleep(.1)
            automatic = bool(automatic)
            after = request("mcpServerStatus/list", status_params)
            refreshed = next(s for s in after["data"] if s["name"] == "blender_domain_probe")
            final = json.loads((RUNTIME / "fixture_state.json").read_text(encoding="utf-8"))
            report = {"codex_cli": subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=10).stdout.strip(),
                      "capability_list_changed": True, "notification_sent": True,
                      "automatic_relist_observed_before_status_query": automatic,
                      "before_tool_count": len(initial["tools"]), "after_tool_count": len(refreshed["tools"]),
                      "fixture_pid_unchanged": ready["pid"] == final["pid"],
                      "fixture_queries": final["list_queries"],
                      "codex_status_cache_updated": len(refreshed["tools"]) == 30,
                      "desktop_active_chat": "not_verified_by_separate_app_server",
                      "no_chat_created": True, "blender_calls": 0,
                      "existing_thread_rollout_unchanged": before_hash == hashlib.sha256(rollout.read_bytes()).hexdigest(),
                      "existing_thread_mtime_unchanged": before_mtime == rollout.stat().st_mtime_ns,
                      "notification_events": observed, "probe_completed": True}
            (RUNTIME / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            assert report["existing_thread_rollout_unchanged"] and report["existing_thread_mtime_unchanged"]
            print(json.dumps({k: v for k, v in report.items() if k not in ("notification_events", "fixture_queries")}, ensure_ascii=False))
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
            proc.stdout.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
