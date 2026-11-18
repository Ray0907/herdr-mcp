"""Error-path E2E: fake binaries, unreachable server, subprocess timeout. Run via test_e2e.py."""
import asyncio, json, os, shutil, subprocess, tempfile, time
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from e2e_common import ALL_TOOLS, call, params


def fake_bin(d, name, body):
    p = Path(d) / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


def emit(stream, obj, code):
    return f"cat <<'EOF' >&{stream}\n{json.dumps(obj)}\nEOF\nexit {code}"


async def failure_modes():
    d = tempfile.mkdtemp(prefix="herdr-mcp-fake-")
    try:
        blocked_ok = {"id": "cli:agent:prompt", "result": {"agent": {"agent_status": "blocked", "name": "x"},
                                                           "type": "agent_prompted"}}
        def err(code, msg):
            return {"error": {"code": code, "message": msg}, "id": "cli:agent:prompt"}
        cases = [
            ("blocked_settle", emit(1, blocked_ok, 0), "blocked"),
            ("agent_blocked", emit(2, err("agent_blocked", "agent x is blocked and requires interactive input"), 1), "agent_blocked"),
            ("agent_prompt_stalled", emit(2, err("agent_prompt_stalled", "no observed working or blocked state"), 1), "agent_prompt_stalled"),
            ("timeout", emit(2, err("timeout", "timed out waiting for agent status"), 1), "timeout"),
        ]
        for name, body, code in cases:
            async with stdio_client(params(HERDR_BIN=fake_bin(d, name, body))) as (rd, wr):
                async with ClientSession(rd, wr) as s:
                    await s.initialize()
                    err_, txt = await call(s, "agent_prompt", {"target": "x", "text": "hi", "timeout_ms": 4000})
                    assert err_, f"{name}: must be isError: {txt}"
                    assert code in txt, f"{name}: {code!r} missing from {txt}"
                    if name == "blocked_settle":
                        assert "needs a human" in txt, txt

        # subprocess timeout: fake sleeps 60, subprocess timeout = 1 s + 10 s
        slow = fake_bin(d, "slow", "exec sleep 60")  # exec: no orphan child holding the pipes
        async with stdio_client(params(HERDR_BIN=slow)) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                t0 = time.time()
                err_, txt = await call(s, "agent_prompt", {"target": "x", "text": "hi", "timeout_ms": 1000})
                assert err_ and "timed out" in txt, txt
                assert time.time() - t0 < 30, "subprocess timeout did not fire"
                assert len((await s.list_tools()).tools) == len(ALL_TOOLS)  # server still answers
        await asyncio.sleep(0.5)
        left = subprocess.run(["pgrep", "-fx", "sleep 60"], capture_output=True, text=True).stdout.split()
        assert not left, f"leftover sleep children: {left}"
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # server unreachable: real binary, nonexistent socket
    sock = "/tmp/claude-501/nope.sock"
    assert not os.path.exists(sock), sock
    async with stdio_client(params(HERDR_SOCKET_PATH=sock)) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            for _ in range(2):
                err_, txt = await call(s, "snapshot", {})
                assert err_ and "server_not_running" in txt, txt
