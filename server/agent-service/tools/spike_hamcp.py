"""S1 item 4 probe (read-only): the live ha-mcp tool surface against its effective policy.

    docker run --rm --network server_default -e HA_MCP_URL --entrypoint python \
        weixue/agent-service:<tag> -m tools.spike_hamcp

Prints tool names, annotations and which tools a policy rule names; never the URL or a token.
Only initialize, tools/list, ping, GET /api/policy/config and GET /api/policy/pending are sent.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/app")

from weixue_hamcp import HaMcpClient  # noqa: E402

POLICY_TOOLS = ("ha_manage_security_policy", "ha_dev_manage_server")


async def main() -> int:
    client = HaMcpClient(os.environ["HA_MCP_URL"])
    out = {}
    started = time.monotonic()
    info = await client.initialize()
    out["initialize_ms"] = int((time.monotonic() - started) * 1000)
    out["server"] = info.get("serverInfo")
    out["protocol"] = info.get("protocolVersion")
    started = time.monotonic()
    out["ping"] = await client.request("ping", {})
    out["ping_ms"] = int((time.monotonic() - started) * 1000)
    tools = await client.list_tools()
    policy = await client.policy_config()
    pending = await client.pending()
    ruled = {r.get("tool_name") for r in policy.get("rules") or []}
    out["tool_count"] = len(tools)
    out["tools"] = [{"name": t["name"], "read_only": bool((t.get("annotations") or {}).get("readOnlyHint")),
                     "destructive": bool((t.get("annotations") or {}).get("destructiveHint")),
                     "ruled": t["name"] in ruled or "*" in ruled} for t in tools]
    out["policy"] = {k: policy.get(k) for k in ("version", "rule_effect", "wait_seconds", "approval_ttl_minutes",
                                                "event_decisions_enabled")}
    out["rules"] = [{"tool_name": r.get("tool_name"), "when": r.get("when"), "remember_minutes": r.get("remember_minutes")}
                    for r in policy.get("rules") or []]
    out["policy_tools_present"] = {name: any(t["name"] == name for t in tools) for name in POLICY_TOOLS}
    out["policy_tools_ruled"] = {name: name in ruled for name in POLICY_TOOLS}
    out["pending_count"] = len(pending)
    out["pending_keys"] = sorted({k for p in pending for k in p})
    print(json.dumps(out, indent=1))
    await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
