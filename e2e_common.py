"""Shared helpers for test_e2e.py and test_failure_modes.py."""
import json, os, subprocess
from pathlib import Path

from mcp import StdioServerParameters

HERE = Path(__file__).parent
HERDR = "/opt/homebrew/bin/herdr"
UV = "/opt/homebrew/bin/uv"
ALL_TOOLS = ["snapshot", "pane_read", "pane_run", "agent_read", "agent_prompt",
             "tab_create", "pane_split", "pane_close", "tab_close", "agent_start"]
log: list[dict] = []
created_ids: set[str] = set()  # every tab/pane id the test made, to assert none survive


def herdr(*args):
    r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"herdr {args} failed: {r.stderr}"
    return json.loads(r.stdout)


def ctrl_c(pane):
    r = subprocess.run([HERDR, "pane", "send-keys", pane, "ctrl+c"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def ids_of(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("pane_id", "tab_id"):
                yield v
            yield from ids_of(v)
    elif isinstance(o, list):
        for v in o:
            yield from ids_of(v)


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
