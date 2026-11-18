# herdr-mcp

A local stdio [MCP](https://modelcontextprotocol.io) server that wraps the
[Herdr](https://herdr.dev) CLI, so Claude Desktop can inspect and drive the
Herdr session running on the same machine.

Tested with herdr 0.9.5, `mcp` 2.3.0, and Claude Desktop on macOS.

## Security

`pane_run`, `agent_prompt`, and `agent_start` execute commands on this machine.
That is the point of the tool, and it is acceptable only because the transport
is local stdio started by Claude Desktop. There is no HTTP transport; do not put
this server behind HTTP or a tunnel. In Claude Desktop, keep these tools on
"ask every time".

`pane_close` and `tab_close` terminate the processes inside and cannot be
undone; see the ownership guard below.

`agent_start` args are not the only thing that decides an agent's permissions.
Your shell aliases and agent config apply too: in practice, `claude --model
opusplan` started through `agent_start` ran with `--dangerously-skip-permissions`
(its resume command showed the flag) because of a shell alias. Check what your
shell turns the agent command into before letting Claude start agents.

## Tools

| Tool | What it does |
|---|---|
| `snapshot()` | Live state as JSON: workspaces, tabs, panes, agents |
| `pane_read(pane_id, lines=120)` | Recent terminal output of a pane |
| `pane_run(pane_id, command)` | Types a command into a pane and presses Enter |
| `agent_read(target, lines=120)` | Recent output of an agent |
| `agent_prompt(target, text, timeout_ms=120000)` | Sends a prompt to an agent and waits for it to settle |
| `tab_create(workspace_id, cwd="", label="")` | Creates a tab; returns its tab ID and root pane ID |
| `pane_split(pane_id, direction, cwd="")` | Splits a pane `right` or `down`; returns the new pane ID |
| `pane_close(pane_id)` | Closes a pane this server created |
| `tab_close(tab_id)` | Closes a tab this server created |
| `agent_start(name, kind, pane_id, args=None)` | Starts a coding agent in a pane at its shell prompt |

New tabs and panes never take focus.

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

## Use in a Claude Project

The server is registered with the Claude Desktop app, not with a single
Project, so its tools are available in Project chats opened in the desktop app.
Projects opened on claude.ai in a browser cannot reach it: a local stdio server
only exists on your machine.

1. Open the Project in Claude Desktop and start a chat.
2. Check that `herdr` is enabled in the chat's tools menu.
3. Ask in plain language, for example:
   - "Take a herdr snapshot and list every pane and agent."
   - "Read the last 50 lines of pane w1:p2."
   - "Ask the agent named reviewer to report its status."

To save repeating yourself, add something like this to the Project's
instructions:

```text
You can control my local Herdr session through the herdr tools.
Call snapshot first to get pane IDs and agent names; never guess them.
Read before you write: use pane_read or agent_read to check a pane's state
before pane_run or agent_prompt.
Ask me before running anything that deletes, overwrites, or pushes.
```

## Error handling

- Any CLI failure becomes a tool error (`isError`) carrying the CLI's JSON as-is
  (`server_not_running`, `agent_blocked`, `agent_prompt_stalled`, `timeout`, ...).
- `agent_prompt` whose turn ends at a question/approval dialog makes the CLI exit
  0 with `agent_status: "blocked"`. The server flags that as an error: the agent
  is waiting for a human; read it with `agent_read`.
- No automatic retry. A `timeout` (CLI or subprocess) does not prove the prompt
  was not delivered, so do not blindly resend.
- Subprocess timeout: 30 s per call; `agent_prompt` uses `timeout_ms` + 10 s.
- `lines` is clamped to 1..2000; `timeout_ms` to 1000..600000.
- Ownership guard: `pane_close`, `tab_close`, and `agent_start` act only on
  panes/tabs this server created since it started. Others are refused.
  `pane_close` and `tab_close` also refuse when any affected pane hosts an agent
  or has a foreground process, and list each busy pane and what it runs. A
  just-created pane can be briefly refused while its shell starts; retry.
- Missing or broken `herdr` binary is a tool error; the server stays up.

## Notes from use in Claude Desktop

- **Agent at a question or approval dialog.** `agent_prompt` returns
  `agent_blocked` and sends nothing. Read the options with `agent_read`, then
  answer with `pane_run` (for example `"1"`). That is deciding on the user's
  behalf: ask the user first.
- **Starting an agent in a new pane.** `agent_start` right after `tab_create` or
  `pane_split` can fail with `agent_pane_busy` while the shell is still
  initialising. Wait a moment and retry. In busy-pane errors the process name
  can show as `[None]` during that window.
- **Closing a pane or tab with an agent.** It is refused while the agent runs.
  End the agent first (for example send `/exit` with `agent_prompt` or
  `pane_run`), then `pane_close` / `tab_close`.
- **Refused by Claude, not by the server.** Claude Desktop's own safety review
  can block destructive calls such as `tab_close` before they reach the server.
  That is not a server error.

## Test

Run from inside a Herdr pane. The test splits that pane, uses the new pane as a
scratch target, and closes what it created afterwards.

```sh
uv run test_e2e.py
```

The error-path tests live in `test_failure_modes.py` (called from `test_e2e.py`);
shared helpers are in `e2e_common.py`.
They use fake `HERDR_BIN` scripts (written to a temp dir, then deleted)
and a nonexistent `HERDR_SOCKET_PATH` to exercise failure modes.
It writes `e2e-transcript.json`, which contains a snapshot of your real session
(pane titles and paths). Keep that file out of version control.
The test hardcodes `/opt/homebrew/bin/herdr` and `/opt/homebrew/bin/uv`.

## Verification status

- Verified by hand from Claude Desktop: `snapshot`, `pane_read`, `pane_run`,
  `agent_read`, `agent_prompt`, `agent_start` (`claude --model opusplan` started
  and replied), `tab_create`, `pane_split`, `pane_close` (including refusal of a
  pane running a process).
- `tab_close`: verified over MCP stdio by `test_e2e.py` (create/close an empty
  `/tmp` tab, refusal of a tab from another server instance, refusal of a tab
  containing a hand-added pane). From Claude Desktop it was blocked by Desktop's
  own safety review, not by the server.
- Verified against the real CLI and covered by fake-binary tests: `timeout`,
  `agent_blocked`, `agent_prompt_stalled`, blocked-settle, `server_not_running`,
  subprocess timeout.
- Not verified from Claude Desktop: `tab_close` (see above).
