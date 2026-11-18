# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3"]
# ///
"""E2E: drive server.py over stdio like Claude Desktop does. Run: uv run test_e2e.py"""
import asyncio, json, os, subprocess, sys, time, uuid

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from e2e_common import ALL_TOOLS, HERDR, HERE, call, created_ids, ctrl_c, herdr, ids_of, log, params
from test_failure_modes import failure_modes

ORCH_PANE = os.environ.get("HERDR_PANE_ID")
if not ORCH_PANE:
    sys.exit("HERDR_PANE_ID not set: run this test from inside a Herdr pane (it splits that pane for its scratch pane)")
TRANSCRIPT = HERE / "e2e-transcript.json"
made: dict[str, str] = {}  # layout created through the server, for cleanup
ws_of: list[str] = []


async def main(pane, other_tab):
    ws = pane.split(":")[0]
    # --- main session
    async with stdio_client(params()) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            listed = (await s.list_tools()).tools
            tools = sorted(t.name for t in listed)
            desc = {t.name: t.description or "" for t in listed}
            for n in ("pane_run", "agent_prompt", "agent_start"):
                assert "EXECUTES" in desc[n], f"{n} description must say EXECUTES"
            for n in ("pane_close", "tab_close"):
                assert "TERMINATES" in desc[n], f"{n} description must say TERMINATES"
            log.append({"tools/list": tools})
            assert tools == sorted(ALL_TOOLS), tools

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

            # layout: validation
            ws = pane.split(":")[0]
            for tool, a in [("tab_create", {"workspace_id": "--help"}),
                            ("tab_create", {"workspace_id": ws, "cwd": "relative/path"}),
                            ("tab_create", {"workspace_id": ws, "label": ""}),
                            ("tab_create", {"workspace_id": ws, "label": "   "}),
                            ("tab_create", {"workspace_id": ws, "label": "a\x01b"}),
                            ("pane_run", {"pane_id": pane, "command": "a\x00b"}),
                            ("agent_start", {"name": "ok", "kind": "claude", "pane_id": pane}),
                            ("pane_split", {"pane_id": pane, "direction": "left"}),
                            ("pane_split", {"pane_id": "--help", "direction": "right"}),
                            ("tab_close", {"tab_id": "--help"}),
                            ("agent_start", {"name": "Bad Name", "kind": "claude", "pane_id": pane}),
                            ("agent_start", {"name": "ok", "kind": "nope", "pane_id": pane})]:
                err, _ = await call(s, tool, a)
                assert err, f"{tool} {a} should error"

            # layout: create, split, close what the server created
            err, txt = await call(s, "tab_create", {"workspace_id": ws, "label": "mcp-e2e"})
            assert not err, txt
            r = json.loads(txt)["result"]
            tab, root = r["tab"]["tab_id"], r["root_pane"]["pane_id"]
            made["tab"] = tab
            created_ids.update([tab, root])
            err, txt = await call(s, "pane_split", {"pane_id": root, "direction": "right"})
            assert not err, txt
            child = json.loads(txt)["result"]["pane"]["pane_id"]
            created_ids.add(child)
            deadline = time.time() + 10  # a just-split shell is briefly "busy" while it starts
            while True:
                err, txt = await call(s, "pane_close", {"pane_id": child})
                if not err:
                    break
                assert "idle shell" in txt and time.time() < deadline, txt
                await asyncio.sleep(0.5)

            # agent_start on a pane the server did not create is refused
            err, txt = await call(s, "agent_start", {"name": "mcpe2e", "kind": "claude", "pane_id": pane})
            assert err and "refused" in txt, txt

            # tab_close refuses a tab holding a pane added by hand
            hand = herdr("pane", "split", "--pane", root, "--direction", "right", "--no-focus")["result"]["pane"]["pane_id"]
            created_ids.add(hand)
            err, txt = await call(s, "tab_close", {"tab_id": tab})
            assert err and "refused" in txt and hand in txt, txt
            assert tab in {p["tab_id"] for p in herdr("pane", "list", "--workspace", ws)["result"]["panes"]}
            herdr("pane", "close", hand)

            # pane_close refuses an owned pane with a foreground process
            err, txt = await call(s, "pane_run", {"pane_id": root, "command": "sleep 30"})
            assert not err, txt
            deadline = time.time() + 10
            while True:
                err, txt = await call(s, "pane_close", {"pane_id": root})
                assert err, "pane_close must refuse a pane running sleep: " + txt
                if "sleep" in txt:
                    break
                assert time.time() < deadline, txt
                await asyncio.sleep(0.5)

            # a second owned tab, left open for the restart check
            err, txt = await call(s, "tab_create", {"workspace_id": ws, "label": "mcp-e2e-restart"})
            assert not err, txt
            r = json.loads(txt)["result"]
            old_tab, old_pane = r["tab"]["tab_id"], r["root_pane"]["pane_id"]
            created_ids.update([old_tab, old_pane])

            async def close_when_idle(sess, tab_id):
                deadline = time.time() + 10  # a fresh shell is briefly "busy" while it starts
                while True:
                    err, txt = await call(sess, "tab_close", {"tab_id": tab_id})
                    if not err:
                        return
                    assert "refused" in txt and time.time() < deadline, txt
                    await asyncio.sleep(0.5)

            # ownership guard: layout this server did not create is refused and survives
            err, txt = await call(s, "pane_close", {"pane_id": pane})
            assert err, txt
            err, txt = await call(s, "tab_close", {"tab_id": other_tab})
            assert err, txt
            ctrl_c(root)  # stop the sleep: tab_close refuses busy panes
            await close_when_idle(s, tab)
            err, txt = await call(s, "snapshot", {})
            assert not err, txt
            live = set(ids(json.loads(txt)))
            assert pane in live, "guarded pane was closed"
            assert child not in live and root not in live, "server-created panes still alive"
            assert f'"{other_tab}"' in txt, "guarded tab was closed"
            assert f'"{tab}"' not in txt, "server-created tab still alive"

            # tab_close refuses a tab with a busy pane, lists it, then succeeds once idle
            err, txt = await call(s, "tab_create", {"workspace_id": ws, "cwd": "/tmp", "label": "mcp-e2e-busy"})
            assert not err, txt
            r = json.loads(txt)["result"]
            u_tab, u_root = r["tab"]["tab_id"], r["root_pane"]["pane_id"]
            created_ids.update([u_tab, u_root])
            deadline = time.time() + 10  # wait for an idle shell
            while True:
                info = herdr("pane", "process-info", "--pane", u_root)["result"]["process_info"]
                if info["foreground_processes"] and all(p.get("pid") == info["shell_pid"] for p in info["foreground_processes"]):
                    break
                assert time.time() < deadline, "scratch shell never became idle"
                await asyncio.sleep(0.5)
            err, txt = await call(s, "pane_run", {"pane_id": u_root, "command": "sleep 30"})
            assert not err, txt
            deadline = time.time() + 10
            while True:
                err, txt = await call(s, "tab_close", {"tab_id": u_tab})
                assert err and "refused" in txt, "tab_close must refuse a busy tab: " + txt
                if "sleep" in txt:
                    break
                assert time.time() < deadline, txt
                await asyncio.sleep(0.5)
            assert u_root in txt, txt
            assert u_tab in {p["tab_id"] for p in herdr("pane", "list", "--workspace", ws)["result"]["panes"]}
            ctrl_c(u_root)
            await close_when_idle(s, u_tab)
            err, txt = await call(s, "snapshot", {})
            assert not err, txt
            assert u_tab not in set(ids_of(json.loads(txt))), "busy tab still alive after sleep stopped"

            # empty scratch tab in /tmp: create, close, gone from a fresh snapshot
            err, txt = await call(s, "tab_create", {"workspace_id": ws, "cwd": "/tmp", "label": "mcp-e2e-empty"})
            assert not err, txt
            r = json.loads(txt)["result"]
            e_tab, e_root = r["tab"]["tab_id"], r["root_pane"]["pane_id"]
            created_ids.update([e_tab, e_root])
            await close_when_idle(s, e_tab)
            err, txt = await call(s, "snapshot", {})
            assert not err, txt
            live = set(ids_of(json.loads(txt)))
            assert e_tab not in live and e_root not in live, "empty tab or its root pane still alive"

            # tab owned by a second concurrent server instance: refused in A, closable in B
            async with stdio_client(params()) as (rd2, wr2):
                async with ClientSession(rd2, wr2) as s2:
                    await s2.initialize()
                    err, txt = await call(s2, "tab_create", {"workspace_id": ws, "cwd": "/tmp", "label": "mcp-e2e-other-server"})
                    assert not err, txt
                    r = json.loads(txt)["result"]
                    b_tab, b_root = r["tab"]["tab_id"], r["root_pane"]["pane_id"]
                    created_ids.update([b_tab, b_root])
                    err, txt = await call(s, "tab_close", {"tab_id": b_tab})
                    assert err and "refused" in txt, txt
                    err, txt = await call(s, "snapshot", {})
                    assert not err, txt
                    assert b_tab in set(ids_of(json.loads(txt))), "other server's tab was closed by A"
                    await close_when_idle(s2, b_tab)
                    err, txt = await call(s, "snapshot", {})
                    assert not err, txt
                    live = set(ids_of(json.loads(txt)))
                    assert b_tab not in live and b_root not in live, "other server's tab still alive after B closed it"

    # --- restart: ids from the first session are no longer owned
    async with stdio_client(params()) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            for tool, a in [("tab_close", {"tab_id": old_tab}), ("pane_close", {"pane_id": old_pane})]:
                err, txt = await call(s, tool, a)
                assert err and "refused" in txt, txt
            assert old_tab in {p["tab_id"] for p in herdr("pane", "list", "--workspace", ws)["result"]["panes"]}

    # --- missing binary: tool error, server stays up
    async with stdio_client(params(HERDR_BIN="/nonexistent")) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            err, txt = await call(s, "snapshot", {})
            assert err and "/nonexistent" in txt, txt
            err, txt = await call(s, "snapshot", {})  # still alive
            assert err, txt
            assert len((await s.list_tools()).tools) == len(ALL_TOOLS)


def warn(r, what):
    if r.returncode != 0:
        print(f"WARNING: cleanup failed to close {what}: {r.stderr.strip()}", file=sys.stderr)


def cleanup(pane):
    def sh(*a):
        return subprocess.run([HERDR, *a], capture_output=True, text=True, timeout=30)
    ws = (pane or ORCH_PANE).split(":")[0]
    try:
        tabs = json.loads(sh("tab", "list", "--workspace", ws).stdout)["result"]["tabs"]
        for t in tabs:
            if t["label"].startswith("mcp-e2e"):
                warn(sh("tab", "close", t["tab_id"]), t["tab_id"])
        if pane:
            warn(sh("pane", "close", pane), pane)
        live = set(ids_of(json.loads(sh("api", "snapshot").stdout)))
    except Exception as e:
        print("cleanup error:", repr(e), file=sys.stderr)
        return 1
    left = created_ids & live
    if left:
        print("FAIL: test layout still alive:", sorted(left), file=sys.stderr)
    return 1 if left else 0


def run():
    pane = other_tab = None
    code = 0
    try:
        pane = herdr("pane", "split", "--pane", ORCH_PANE, "--direction", "down", "--no-focus")["result"]["pane"]["pane_id"]
        # a tab the server did not create, as the target for the ownership guard
        other_tab = herdr("tab", "create", "--workspace", pane.split(":")[0], "--label", "mcp-e2e-other",
                          "--no-focus")["result"]["tab"]["tab_id"]
        created_ids.update([pane, other_tab])
        log.append({"scratch_pane": pane, "scratch_tab": other_tab})
        asyncio.run(main(pane, other_tab))
        asyncio.run(failure_modes())
    except BaseException as e:
        log.append({"failure": repr(e)})
        print("FAIL:", repr(e), file=sys.stderr)
        code = 1
    finally:
        code = cleanup(pane) or code
        TRANSCRIPT.write_text(json.dumps(log, indent=2))
    print("PASS" if code == 0 else "FAILED")
    return code


if __name__ == "__main__":
    sys.exit(run())
