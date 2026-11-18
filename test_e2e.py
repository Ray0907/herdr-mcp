# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3"]
# ///
"""E2E: drive server.py over stdio like Claude Desktop does. Run: uv run test_e2e.py"""
import asyncio, json, os, subprocess, sys, time, uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
HERDR = "/opt/homebrew/bin/herdr"
UV = "/opt/homebrew/bin/uv"
ORCH_PANE = os.environ.get("HERDR_PANE_ID")
if not ORCH_PANE:
    sys.exit("HERDR_PANE_ID not set: run this test from inside a Herdr pane (it splits that pane for its scratch pane)")
TRANSCRIPT = HERE / "e2e-transcript.json"
log: list[dict] = []


def herdr(*args):
    r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"herdr {args} failed: {r.stderr}"
    return json.loads(r.stdout)


def env(**extra):
    # minimal env like Claude Desktop: no HERDR_* vars
    e = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    e.update(extra)
    return e


def params(**extra):
    return StdioServerParameters(command=UV, args=["run", str(HERE / "server.py")], env=env(**extra))


async def call(s, name, args):
    entry = {"tool": name, "args": args}
    log.append(entry)
    r = await s.call_tool(name, args)
    entry["isError"] = bool(r.is_error)
    entry["text"] = "".join(getattr(c, "text", "") for c in r.content)
    return r.is_error, entry["text"]


async def main(pane):
    # --- main session
    async with stdio_client(params()) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            listed = (await s.list_tools()).tools
            tools = sorted(t.name for t in listed)
            desc = {t.name: t.description or "" for t in listed}
            for n in ("pane_run", "agent_prompt"):
                assert "EXECUTES" in desc[n], f"{n} description must say EXECUTES"
            log.append({"tools/list": tools})
            assert tools == sorted(["snapshot", "pane_read", "pane_run", "agent_read", "agent_prompt"]), tools

            err, txt = await call(s, "snapshot", {})
            assert not err, txt
            json.loads(txt)
            def ids(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k == "pane_id":
                            yield v
                        yield from ids(v)
                elif isinstance(o, list):
                    for v in o:
                        yield from ids(v)
            assert pane in set(ids(json.loads(txt))), "scratch pane missing from snapshot"

            marker = f"herdr-mcp-{uuid.uuid4().hex[:8]}"
            err, txt = await call(s, "pane_run", {"pane_id": pane, "command": f"echo {marker}"})
            assert not err, txt
            # leading-dash command is passed through, not eaten as a flag
            err, txt = await call(s, "pane_run", {"pane_id": pane, "command": "-dashcmd"})
            assert not err, txt
            deadline = time.time() + 10
            while True:
                err, txt = await call(s, "pane_read", {"pane_id": pane, "lines": 50})
                assert not err, txt
                if marker in {l.strip() for l in txt.splitlines()} and "-dashcmd" in txt:
                    break
                assert time.time() < deadline, f"marker not seen: {txt[-300:]}"
                await asyncio.sleep(0.5)

            # validation
            for bad in ["--help", "nope", "-x:p1", ""]:
                err, txt = await call(s, "pane_read", {"pane_id": bad})
                assert err, f"pane_read {bad!r} should error"
            err, _ = await call(s, "pane_run", {"pane_id": "--help", "command": "echo x"})
            assert err
            err, _ = await call(s, "pane_read", {"pane_id": pane, "lines": 999999})
            assert not err  # clamped, not rejected
            err, txt = await call(s, "pane_read", {"pane_id": pane, "lines": 0})
            assert not err, txt

            # agent_*: validation + error path (no agent in scratch pane)
            for tool, a in [("agent_read", {"target": "--help"}), ("agent_read", {"target": "Bad Name"}),
                            ("agent_prompt", {"target": "--help", "text": "hi"})]:
                err, _ = await call(s, tool, a)
                assert err, f"{tool} {a} should error"
            err, txt = await call(s, "agent_read", {"target": pane})
            assert err, txt
            err, txt = await call(s, "agent_prompt", {"target": pane, "text": "-hello", "timeout_ms": 3000})
            assert err, txt
            assert "unknown option" not in txt, "leading '-' leaked to CLI parser: " + txt

    # --- missing binary: tool error, server stays up
    async with stdio_client(params(HERDR_BIN="/nonexistent")) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            err, txt = await call(s, "snapshot", {})
            assert err and "/nonexistent" in txt, txt
            err, txt = await call(s, "snapshot", {})  # still alive
            assert err, txt
            assert len((await s.list_tools()).tools) == 5


def run():
    pane = None
    try:
        pane = herdr("pane", "split", "--pane", ORCH_PANE, "--direction", "down", "--no-focus")["result"]["pane"]["pane_id"]
        log.append({"scratch_pane": pane})
        asyncio.run(main(pane))
        print("PASS")
        return 0
    except BaseException as e:
        log.append({"failure": repr(e)})
        print("FAIL:", repr(e), file=sys.stderr)
        return 1
    finally:
        if pane:
            subprocess.run([HERDR, "pane", "close", pane], capture_output=True)
        TRANSCRIPT.write_text(json.dumps(log, indent=2))


if __name__ == "__main__":
    sys.exit(run())
