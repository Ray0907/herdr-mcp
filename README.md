# herdr-mcp

A local stdio [MCP](https://modelcontextprotocol.io) server that wraps the
[Herdr](https://herdr.dev) CLI, so Claude Desktop can inspect and drive the
Herdr session running on the same machine.

Tested with herdr 0.9.5, `mcp` 2.3.0, and Claude Desktop on macOS.

## Security

`pane_run` and `agent_prompt` execute arbitrary commands on your machine. That
is the point of the tool, and it is acceptable only because the transport is
local stdio started by Claude Desktop. Do not put this server behind HTTP or a
tunnel. In Claude Desktop, keep these two tools on "ask every time".

## Tools

| Tool | What it does |
|---|---|
| `snapshot()` | Live state as JSON: workspaces, tabs, panes, agents |
| `pane_read(pane_id, lines=120)` | Recent terminal output of a pane |
| `pane_run(pane_id, command)` | Types a command into a pane and presses Enter |
| `agent_read(target, lines=120)` | Recent output of an agent |
| `agent_prompt(target, text, timeout_ms=120000)` | Sends a prompt to an agent and waits for it to settle |

`pane_id` looks like `w1:p2`. `target` is a pane ID or an agent name. Get both
from `snapshot`; the server does not run inside a pane, so there is no "current"
pane.

## Install

Requires [uv](https://docs.astral.sh/uv/) and a running Herdr server.

Clone the repo, then add the server to
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "herdr": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "/path/to/herdr-mcp/server.py"]
    }
  }
}
```

Use absolute paths: Claude Desktop starts servers with a minimal `PATH`. Quit
and reopen Claude Desktop. `uv` installs the one dependency on first run.

The server calls `/opt/homebrew/bin/herdr` by default. Set `HERDR_BIN` to use
another location:

```json
"env": { "HERDR_BIN": "/usr/local/bin/herdr" }
```

## Test

Run from inside a Herdr pane. The test splits that pane, uses the new pane as a
scratch target, and closes it afterwards.

```sh
uv run test_e2e.py
```

It writes `e2e-transcript.json`, which contains a snapshot of your real session
(pane titles and paths). Keep that file out of version control.

The test hardcodes `/opt/homebrew/bin/herdr` and `/opt/homebrew/bin/uv`.

## Not covered by the test

- `agent_prompt` reaching a real agent (checked by hand through Claude Desktop)
- `agent_blocked` and `agent_prompt_stalled` responses
- Subprocess timeout
