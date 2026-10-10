"""Start the Blender MCP bridge inside a running Blender.

Three supported entry points
----------------------------
1. **Open Blender, run this file** (Text Editor -> Open -> Run Script, or the
   Python console)::

       exec(compile(open(r"<workspace>\\MCP\\blender\\bootstrap.py").read(),
                    "bootstrap.py", "exec"), {"__name__": "__main__"})

   or simply, from the console::

       import sys; sys.path.insert(0, r"<workspace>\\MCP\\blender")
       import bootstrap; bootstrap.main([])

2. **Launch Blender from a shell with the bridge enabled**::

       blender.exe --python "<workspace>\\MCP\\blender\\bootstrap.py" -- --mcp-port 9876

3. **Headless (no event loop)**::

       blender.exe --background --factory-startup \\
           --python "<workspace>\\MCP\\blender\\bootstrap.py" -- --mcp-pump

   In ``--background`` mode there is no event loop, so ``bpy.app.timers`` never
   fires; the bootstrap pumps the very same non-blocking socket loop from a
   plain Python loop instead.  This is the documented headless harness path.

Only the Python standard library bundled with Blender plus ``bpy`` is used.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 9876,
    "token_file": None,
    "project_dir": None,
    "pump": None,          # None = auto (yes when Blender runs in background)
    "pump_seconds": 0.0,   # 0 = pump forever
    "force": False,
    "no_timer": False,
    "quiet": False,
}

USAGE = """bootstrap options (pass after '--'):
  --mcp-host HOST          bind address (default 127.0.0.1)
  --mcp-port PORT          TCP port (default 9876)
  --mcp-token-file PATH    shared token file (default <workspace>/MCP/.runtime/token)
  --mcp-project-dir PATH   project root for save_blend (default <workspace>/Project)
  --mcp-pump               force the manual pump loop (required in --background)
  --mcp-pump-seconds N     stop pumping after N seconds (0 = forever)
  --mcp-force              start our own bridge even if one already answers
  --mcp-no-timer           do not register a bpy.app.timers poller
  --mcp-quiet              only print the READY line
"""


def parse_args(argv=None) -> dict:
    """Parse our own ``--mcp-*`` flags, ignoring every other Blender argument."""
    argv = list(sys.argv if argv is None else argv)
    options = dict(DEFAULTS)
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = [a for a in argv if a.startswith("--mcp-")]
    index = 0
    while index < len(argv):
        arg = argv[index]
        index += 1
        if arg in ("-h", "--help", "--mcp-help"):
            options["help"] = True
            continue
        if not arg.startswith("--mcp-"):
            continue
        name = arg[len("--mcp-"):]
        value = None
        if "=" in name:
            name, value = name.split("=", 1)
        key = name.replace("-", "_")
        if key in ("pump", "force", "no_timer", "quiet"):
            options[key] = True if value is None else value.lower() not in ("0", "false", "no")
            continue
        if key == "pump_seconds":
            if value is None:
                if index >= len(argv) or argv[index].startswith("--"):
                    raise ValueError("--mcp-pump-seconds needs a number")
                value = argv[index]
                index += 1
            options[key] = float(value)
            if options[key] < 0:
                raise ValueError("--mcp-pump-seconds must be non-negative")
            continue
        if value is None:
            if index < len(argv) and not argv[index].startswith("--"):
                value = argv[index]
                index += 1
            else:
                print("[blender-mcp] option %s needs a value" % arg)
                continue
        if key == "port":
            options[key] = int(value)
        elif key in ("host", "token_file", "project_dir"):
            options[key] = value
    return options


def main(argv=None) -> int:
    options = parse_args(argv)
    if options.get("help"):
        print(USAGE)
        return 0

    import bpy  # noqa: PLC0415 - only available inside Blender

    if str(ADDON_DIR) not in sys.path:
        sys.path.insert(0, str(ADDON_DIR))
    import blender_mcp_addon as addon  # noqa: PLC0415

    # Running this supported bootstrap in an existing Blender refreshes only
    # the isolated file and Outliner commands. Keep the live listener/timer, token, scene and
    # all other registered commands intact; do not reload or stop the add-on.
    import importlib
    import file_open
    file_open = importlib.reload(file_open)
    file_open.register_commands(addon)
    import outliner
    outliner = importlib.reload(outliner)
    helpers = outliner.register_commands(addon)
    import outliner_management
    outliner_management = importlib.reload(outliner_management)
    outliner_management.register_commands(addon, helpers)

    # Refresh isolated review commands without re-registering UI classes or
    # interrupting the resident listener. Abort borrowed UI sessions first.
    import review_capture_common
    for session in list(review_capture_common._SESSIONS.values()):
        review_capture_common.restore_session(session, reason="bootstrap_refresh")
    review_capture_common = importlib.reload(review_capture_common)
    import properties
    import geometry_nodes_edit
    import model_inspection
    import geometry_nodes_capture
    for module in (properties, geometry_nodes_edit, model_inspection, geometry_nodes_capture):
        module = importlib.reload(module)
        module.register_commands(addon)
    import review_maintenance
    review_maintenance = importlib.reload(review_maintenance)
    review_maintenance.register_commands(addon)

    quiet = bool(options["quiet"])
    log = addon._Logger(addon._log_path())

    def say(message):
        if not quiet:
            print("[blender-mcp] %s" % message)

    if options["project_dir"]:
        os.environ["BLENDER_MCP_PROJECT_DIR"] = str(options["project_dir"])
    token_path = Path(options["token_file"]) if options["token_file"] else None
    host = options["host"]
    port = int(options["port"])

    token = addon.read_or_create_token(token_path)

    # This script and the resident timer both run on Blender's main thread.
    # A blocking TCP health probe against our own listener cannot be serviced
    # until this script returns. Reuse the live in-process server directly.
    resident = getattr(addon, "_SERVER", None)
    if resident is not None and resident.running:
        import hmac
        if (resident.host != addon.normalize_loopback_host(host) or resident.port != port or
                not hmac.compare_digest(resident._token, token)):
            raise RuntimeError("A bridge is already running in this Blender with different settings; leave it running and use its existing configuration")
        say("reusing the bridge already running in this Blender on %s" % resident.address)
        print("READY reused %s" % resident.address)
        return 0

    # Reuse a healthy bridge instead of stacking a second listener on the port.
    if not options["force"]:
        existing = addon.probe_existing_bridge(host, port, token, timeout=1.0)
        if existing:
            say(
                "an existing healthy bridge already answers on %s:%d (Blender %s, pid %s) - reusing it"
                % (host, port, existing.get("blender_version"), existing.get("pid"))
            )
            print("READY reused %s:%d" % (host, port))
            return 0
    else:
        say("--mcp-force: starting our own bridge even if %s:%d answers" % (host, port))

    # A healthy resident add-on already owns its registered UI classes. Probe
    # first so rerunning this entry never registers them again (Blender 5.2
    # raises ValueError for duplicate register_class, not RuntimeError).
    addon.register()

    pump = options["pump"]
    if pump is None:
        pump = bool(bpy.app.background)
    use_timer = not options["no_timer"] and not pump

    try:
        server = addon.start_bridge(
            host=host,
            port=port,
            token=token,
            token_path=token_path,
            use_timer=use_timer,
            log=log,
        )
    except addon.CommandError as exc:
        print("[blender-mcp] ERROR %s: %s" % (exc.code, exc.message))
        print("BLENDER_MCP_FAILED %s" % exc.code)
        return 2

    say("bridge listening on %s (protocol v%d)" % (server.address, addon.PROTOCOL_VERSION))
    say("token file: %s (contents are never printed)" % (token_path or addon.default_token_path()))
    say("project root: %s" % addon.project_dir())
    print("READY %s" % server.address)

    if pump:
        pump_seconds = float(options["pump_seconds"] or 0.0)
        say(
            "headless pump loop active%s (bpy.app.timers does not fire in --background)"
            % (" for %ss" % pump_seconds if pump_seconds else " until shutdown/KeyboardInterrupt")
        )
        try:
            result = addon.pump(seconds=pump_seconds or None)
        except KeyboardInterrupt:
            result = {"stopped": "keyboard_interrupt"}
        addon.stop_bridge("pump loop finished")
        say("pump loop finished: %s" % result)
        return 0

    say("GUI mode: the bridge is polled by bpy.app.timers on the main thread")
    say("stop it with bpy.ops.blendermcp.stop_bridge() or Properties > Scene > Blender MCP Bridge")
    return 0


if __name__ == "__main__":
    # Blender treats SystemExit(0) from a --python script as a request to quit
    # the whole application. In GUI mode main() merely registers the timer;
    # returning normally is essential so the window/event loop stays alive.
    # Background mode exits naturally after its bounded/manual pump returns.
    status = main()
    if status:
        raise RuntimeError("Blender MCP bootstrap failed (status %d); see BLENDER_MCP_FAILED above" % status)
