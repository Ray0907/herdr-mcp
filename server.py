# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3"]
# ///
"""Local stdio MCP server wrapping the herdr CLI. No HTTP, ever: pane_run and
agent_prompt execute commands on this machine, safe only because transport is
local stdio launched by Claude Desktop. Never print to stdout (protocol channel)."""
import json
import os
import re
import subprocess

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

HERDR_BIN = os.environ.get("HERDR_BIN", "/opt/homebrew/bin/herdr")
PANE_RE = re.compile(r"^[A-Za-z0-9]+:p[A-Za-z0-9]+$")  # real ids include e.g. w1:pA
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TAB_RE = re.compile(r"^[A-Za-z0-9]+:t[A-Za-z0-9]+$")
WS_RE = re.compile(r"^[A-Za-z0-9]+$")
KINDS = set("pi claude codex gemini cursor devin agy cline omp mastracode opencode copilot kimi kiro droid "
            "amp grok hermes kilo qodercli qwen letta maki muse".split())  # from `herdr agent`, 0.9.5

# ponytail: ownership lives in memory, so layout created before a restart of this
# server can no longer be closed through it. Persist to a file if that hurts.
owned_panes: set[str] = set()
owned_tabs: set[str] = set()

mcp = MCPServer("herdr")


def herdr(*args: str, timeout: float = 30) -> str:
    if any("\0" in a for a in args):
        raise ToolError("NUL byte in argument")
    try:
        r = subprocess.run([HERDR_BIN, *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except OSError as e:
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


def parse_created(out: str, what: str, *path: str):
    try:
        d = json.loads(out)["result"]
        for k in path:
            d = d[k]
        return d
    except (ValueError, KeyError, TypeError) as e:
        raise ToolError(f"{what} CLI succeeded but output was unparseable ({e!r}); a {what} may have been "
                        f"created. Raw output: {out!r}")


def layout_opts(cwd: str, label: str | None = None) -> list[str]:
    opts = ["--no-focus"]
    if cwd:
        if not cwd.startswith(("/", "~")):
            raise ToolError(f"invalid cwd {cwd!r}; must be absolute or start with ~")
        opts += ["--cwd", cwd]
    if label is not None:
        if not label.strip() or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ToolError("invalid label; must be non-blank with no control characters")
        opts += ["--label", label]
    return opts


def clamp(n: int) -> str:
    return str(max(1, min(2000, int(n))))


def tab_panes(tab_id: str) -> list[dict]:
    panes = json.loads(herdr("pane", "list", "--workspace", tab_id.split(":")[0]))["result"]["panes"]
    return [p for p in panes if p["tab_id"] == tab_id]


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
    out = herdr("agent", "prompt", check_target(target), text, "--wait", "--timeout", str(t),
                timeout=t / 1000 + 10)
    try:
        blocked = json.loads(out)["result"]["agent"]["agent_status"] == "blocked"
    except (ValueError, KeyError, TypeError):
        blocked = False
    if blocked:  # CLI exits 0 when the turn ends at a question/approval dialog; not a success
        raise ToolError(f"{out.strip()}\nagent is waiting at an approval/question dialog and needs a human; "
                        "read it with agent_read. Not retried.")
    return out


@mcp.tool()
def tab_create(workspace_id: str, cwd: str = "", label: str | None = None) -> str:
    """Create a tab in a workspace (workspace id like w1) without taking focus. Returns the new tab id
    and its root pane id. cwd must be absolute or start with ~."""
    if not WS_RE.fullmatch(workspace_id):
        raise ToolError(f"invalid workspace id {workspace_id!r}; expected like w1")
    out = herdr("tab", "create", "--workspace", workspace_id, *layout_opts(cwd, label))
    tab_id = parse_created(out, "tab", "tab", "tab_id")
    root = parse_created(out, "tab", "root_pane", "pane_id")
    owned_tabs.add(tab_id)
    owned_panes.add(root)
    return out


@mcp.tool()
def pane_split(pane_id: str, direction: str, cwd: str = "") -> str:
    """Split a pane to the right or down without taking focus. Returns the new pane id."""
    if direction not in ("right", "down"):
        raise ToolError(f"invalid direction {direction!r}; expected right or down")
    out = herdr("pane", "split", "--pane", check_pane(pane_id), "--direction", direction, *layout_opts(cwd))
    owned_panes.add(parse_created(out, "pane", "pane", "pane_id"))
    return out


def busy_panes(pane_ids: list[str]) -> list[str]:
    """Panes not at an idle shell (hosting an agent, or a foreground process besides the shell), as
    'pane running [...]' / 'pane hosts agent ...' strings. Shared by pane_close and tab_close."""
    busy = []
    for pid in pane_ids:
        pane = json.loads(herdr("pane", "get", pid))["result"]["pane"]
        if pane.get("agent"):
            busy.append(f"{pid} hosts agent {pane['agent']!r}")
            continue
        info = json.loads(herdr("pane", "process-info", "--pane", pid))["result"]["process_info"]
        cmds = [p.get("cmdline") for p in info["foreground_processes"] if p.get("pid") != info["shell_pid"]]
        if cmds:
            busy.append(f"{pid} running {cmds}")
    return busy


@mcp.tool()
def pane_close(pane_id: str) -> str:
    """TERMINATES the processes in a pane and closes it. Cannot be undone. Only panes created by this
    server since it started (via pane_split or tab_create) can be closed; any other pane is refused, as is
    a pane hosting an agent or running anything but its idle shell (foreground process)."""
    if check_pane(pane_id) not in owned_panes:
        raise ToolError(f"refused: pane {pane_id} was not created by this server; close it in Herdr yourself")
    pane = json.loads(herdr("pane", "get", pane_id))["result"]["pane"]
    busy = busy_panes([pane_id])
    if busy:
        raise ToolError(f"refused: not at an idle shell: {'; '.join(busy)}")
    out = herdr("pane", "close", pane_id)
    owned_panes.discard(pane_id)
    # closing the last pane of a tab closes the tab
    tab = pane.get("tab_id")
    if tab in owned_tabs and not tab_panes(tab):
        owned_tabs.discard(tab)
    return out


@mcp.tool()
def tab_close(tab_id: str) -> str:
    """TERMINATES every process in a tab and closes it. Cannot be undone. Only tabs created by this
    server since it started (via tab_create) can be closed, and only if every pane in the tab was also
    created by this server; a tab containing any other pane is refused, as is a tab with any pane hosting an
    agent or running anything but its idle shell (foreground process); the error lists each busy pane."""
    if not TAB_RE.fullmatch(tab_id):
        raise ToolError(f"invalid tab id {tab_id!r}; expected like w1:t2")
    if tab_id not in owned_tabs:
        raise ToolError(f"refused: tab {tab_id} was not created by this server; close it in Herdr yourself")
    members = [p["pane_id"] for p in tab_panes(tab_id)]
    foreign = [p for p in members if p not in owned_panes]
    if foreign:
        raise ToolError(f"refused: tab {tab_id} contains panes not created by this server: {foreign}")
    busy = busy_panes(members)
    if busy:
        raise ToolError(f"refused: tab {tab_id} has panes not at an idle shell: {'; '.join(busy)}")
    out = herdr("tab", "close", tab_id)
    owned_tabs.discard(tab_id)
    owned_panes.difference_update(members)
    return out


@mcp.tool()
def agent_start(name: str, kind: str, pane_id: str, args: list[str] | None = None) -> str:
    """EXECUTES a coding agent that can run commands on this machine, in a pane created by this server that
    is sitting at its shell prompt. args are passed to the agent itself after `--` and control its
    permissions (e.g. ["--dangerously-skip-permissions"] would let it run anything unprompted). name must
    be unique among live agents (lowercase, like reviewer). Panes not created by this server are refused."""
    if not AGENT_RE.fullmatch(name):
        raise ToolError(f"invalid agent name {name!r}; expected like reviewer")
    if kind not in KINDS:
        raise ToolError(f"invalid kind {kind!r}; expected one of {sorted(KINDS)}")
    if check_pane(pane_id) not in owned_panes:
        raise ToolError(f"refused: pane {pane_id} was not created by this server")
    if args and any(not a for a in args):
        raise ToolError("empty-string args are not allowed")
    extra = ["--", *args] if args else []
    return herdr("agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "60000",
                 *extra, timeout=70)


if __name__ == "__main__":
    mcp.run(transport="stdio")
