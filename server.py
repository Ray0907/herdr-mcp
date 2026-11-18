# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3"]
# ///
"""Local stdio MCP server wrapping the herdr CLI. No HTTP, ever: pane_run and
agent_prompt execute commands on this machine, safe only because transport is
local stdio launched by Claude Desktop. Never print to stdout (protocol channel)."""
import os
import re
import subprocess

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

HERDR_BIN = os.environ.get("HERDR_BIN", "/opt/homebrew/bin/herdr")
PANE_RE = re.compile(r"^[A-Za-z0-9]+:p[A-Za-z0-9]+$")  # real ids include e.g. w1:pA
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

mcp = MCPServer("herdr")


def herdr(*args: str, timeout: float = 30) -> str:
    try:
        r = subprocess.run([HERDR_BIN, *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, PermissionError) as e:
        raise ToolError(f"herdr binary unusable ({HERDR_BIN}): {e}")
    except subprocess.TimeoutExpired:
        raise ToolError(f"herdr timed out after {timeout}s; command may still have taken effect")
    if r.returncode != 0:
        # CLI puts JSON errors (agent_blocked, timeout, ...) on stdout or stderr; return as-is
        out = "\n".join(x for x in (r.stderr.strip(), r.stdout.strip()) if x)
        raise ToolError(out or f"herdr exited {r.returncode}")
    return r.stdout


def check_pane(v: str) -> str:
    if not PANE_RE.fullmatch(v):
        raise ToolError(f"invalid pane id {v!r}; expected like w1:p2")
    return v


def check_target(v: str) -> str:
    if not (PANE_RE.fullmatch(v) or AGENT_RE.fullmatch(v)):
        raise ToolError(f"invalid target {v!r}; expected pane id or agent name")
    return v


def clamp(n: int) -> str:
    return str(max(1, min(2000, int(n))))


@mcp.tool()
def snapshot() -> str:
    """Live Herdr state (workspaces, tabs, panes, agents) as JSON."""
    return herdr("api", "snapshot")


@mcp.tool()
def pane_read(pane_id: str, lines: int = 120) -> str:
    """Read recent terminal output of a pane (pane id like w1:p2). lines clamped to 1..2000."""
    return herdr("pane", "read", check_pane(pane_id), "--source", "recent-unwrapped", "--lines", clamp(lines))


@mcp.tool()
def pane_run(pane_id: str, command: str) -> str:
    """EXECUTES a shell command on this machine: types `command` into the pane and presses Enter."""
    # verified against herdr 0.9.5: leading '-' text is accepted as positional;
    # `--` would be typed literally into the pane, so it is NOT used.
    return herdr("pane", "run", check_pane(pane_id), command)


@mcp.tool()
def agent_read(target: str, lines: int = 120) -> str:
    """Read recent terminal output of an agent (agent name or pane id). lines clamped to 1..2000."""
    return herdr("agent", "read", check_target(target), "--source", "recent-unwrapped", "--lines", clamp(lines))


@mcp.tool()
def agent_prompt(target: str, text: str, timeout_ms: int = 120000) -> str:
    """EXECUTES commands: sends text as a prompt to a running agent, which will run commands on this machine.
    Waits for idle/done/blocked. On agent_blocked / agent_prompt_stalled / timeout an error with the CLI's
    JSON is returned; a timeout does not prove the prompt was not delivered, so do not blindly retry."""
    # verified against a live agent, herdr 0.9.5: text "-hello" and "--wait" are delivered verbatim.
    t = max(1000, min(600000, int(timeout_ms)))
    return herdr("agent", "prompt", check_target(target), text, "--wait", "--timeout", str(t),
                 timeout=t / 1000 + 10)


if __name__ == "__main__":
    mcp.run()
