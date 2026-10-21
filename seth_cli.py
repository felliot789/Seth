"""Start, stop, inspect, and install Seth's user services. The VM is external."""

import argparse
from contextlib import ExitStack, closing, contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
UNITS = ("seth.target", "seth-listener.service", "seth-browser.service")
COMPONENTS = {"listener": UNITS[1], "browser": UNITS[2]}
MODULES = {"requests": "requests", "PIL": "Pillow", "dotenv": "python-dotenv",
           "evdev": "evdev", "PyQt6": "PyQt6", "websocket": "websocket-client"}


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def installation_file():
    return config_home() / "seth/install.json"


def installation():
    try:
        return json.loads(installation_file().read_text())
    except FileNotFoundError:
        return {}


def systemctl(*args, check=True, capture=False):
    return subprocess.run(["systemctl", "--user", *args], check=check, text=True,
                          capture_output=capture, timeout=310)


def unit_state(unit):
    result = systemctl("show", unit, "--property=LoadState,ActiveState,SubState,MainPID", "--no-pager",
                       check=False, capture=True)
    if result.returncode:
        return {"LoadState": "not-found", "ActiveState": "unavailable", "SubState": "unavailable"}
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def systemd_argument(value, *, command=True):
    """Quote a single literal argument, including systemd's own expansions."""
    value = str(value)
    if any(c in value for c in "\n\r\0"):
        raise ValueError("Installation paths must not contain newlines or NUL bytes.")
    if not command:
        # WorkingDirectory is a single path value, not an ExecStart word list.
        return value.replace("%", "%%")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + value.replace("$", "$$") + '"'


def render_units(root, config):
    values = {"ROOT": root, "PYTHON": root / ".venv/bin/python", "CONFIG": config,
              "MAIN": root / "main.py", "BROWSER": root / "browser_service.py"}
    units = {}
    for name in UNITS:
        content = (root / "deploy" / name).read_text()
        for key, value in values.items():
            content = content.replace("@" + key + "@", systemd_argument(value, command=key != "ROOT"))
        units[name] = content
    return units


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def command_lock():
    directory = config_home() / "seth"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".command.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def install(config):
    command = Path.home() / ".local/bin/seth"
    if command.exists() and not command.is_symlink():
        raise ValueError(f"{command} already exists and is not an installation symlink.")
    destination = config_home() / "systemd/user"
    destination.mkdir(parents=True, exist_ok=True)
    rendered = render_units(ROOT, config)
    listener = destination / COMPONENTS["listener"]
    changed = not listener.exists() or listener.read_text() != rendered[listener.name]
    running = unit_state(COMPONENTS["listener"]).get("ActiveState") == "active"
    previous = installation()
    for name, content in rendered.items():
        path = destination / name
        if not path.exists() or path.read_text() != content:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(content)
            temporary.replace(path)
    command.parent.mkdir(parents=True, exist_ok=True)
    if command.is_symlink():
        if command.resolve() != ROOT / "seth":
            command.unlink()
    if not command.is_symlink():
        command.symlink_to(ROOT / "seth")
    systemctl("daemon-reload")
    systemctl("enable", UNITS[0])
    write_json(installation_file(), {"root": str(ROOT), "config": str(config),
               "restart_listener": bool(running and changed or previous.get("restart_listener", False))})
    print("Installed seth and enabled desktop autostart. Run: seth up")
    if str(command.parent) not in os.get_exec_path():
        print(f"Add {command.parent} to PATH, or run {ROOT / 'seth'} up.")
    return 0


def require_installation(config):
    saved = installation()
    if saved.get("root") != str(ROOT) or saved.get("config") != str(config):
        raise ValueError(f"Install this checkout and configuration first: {ROOT / 'seth'} --config {config} install")
    if any(not (config_home() / "systemd/user" / unit).is_file() for unit in UNITS):
        raise ValueError("Service definitions are missing. Run seth install again.")
    return saved


def import_desktop_environment():
    names = [name for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                              "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE") if os.environ.get(name)]
    if names:
        systemctl("import-environment", *names)


def api_status(config, *, probe=False):
    from ai_client import SethAIClient
    from inference import AIError
    try:
        client = SethAIClient(config)
        client.validate_config()
    except (AIError, TypeError, ValueError):
        return {"status": "not configured", "detail": "Check API settings in .env and config.json"}
    if not probe:
        return {"status": "configured", "detail": "Use seth doctor to check connectivity"}
    import requests
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.head(client.endpoint, timeout=(3, 3), allow_redirects=False) as response:
                code = response.status_code
        return {"status": "reachable" if code < 500 else "unavailable",
                "detail": f"HTTP {code}; credentials and inference not tested"}
    except requests.RequestException:
        return {"status": "unavailable", "detail": "Connection or TLS check failed; no inference request sent"}


def collect_status(config, *, probe=False):
    from browser_service import configured_providers, status_file
    services = {name: unit_state(unit) for name, unit in {"application": UNITS[0], **COMPONENTS}.items()}
    active = services["browser"].get("ActiveState") == "active"
    try:
        snapshot = json.loads(status_file(config).read_text())
    except (OSError, ValueError):
        snapshot = {}
    fresh = (0 <= time.time() - snapshot.get("updated_at", 0) < 45
             and str(snapshot.get("manager_pid")) == services["browser"].get("MainPID"))
    providers = {}
    for provider in configured_providers(config):
        if active and fresh:
            providers[provider] = snapshot.get("providers", {}).get(provider, {"status": "starting", "detail": ""})
        else:
            providers[provider] = {"status": "starting" if active else "stopped",
                                   "detail": "Waiting for browser manager status" if active else "Run seth up"}
    return {"services": services, "providers": providers, "api": api_status(config, probe=probe)}


def print_status(status, *, as_json=False):
    if as_json:
        print(json.dumps(status, indent=2))
        return
    for name, state in status["services"].items():
        active, sub = state.get("ActiveState", "unknown"), state.get("SubState", "unknown")
        label = "ready" if active == "active" else f"{active} ({sub})"
        if name == "application" and active == "active" and any(
                s.get("ActiveState") != "active" for n, s in status["services"].items() if n != "application"):
            label = "degraded"
        print(f"{name.capitalize():12} {label}")
    for name, state in status["providers"].items():
        print(f"{name.capitalize():12} {state['status']} — {state.get('detail', '')}")
    print(f"{'API':12} {status['api']['status']} — {status['api']['detail']}")


def missing_dependencies():
    return [package for module, package in MODULES.items() if importlib.util.find_spec(module) is None]


def doctor(config):
    status = collect_status(config, probe=True)
    print_status(status)
    issues = []
    if any(state.get("ActiveState") != "active" for state in status["services"].values()):
        issues.append("Start missing services with seth up; inspect failures with seth logs.")
    if status["api"]["status"] not in ("configured", "reachable"):
        issues.append("Repair the API configuration or endpoint; browser providers remain independent.")
    for name, state in status["providers"].items():
        if state["status"] not in ("ready", "busy"):
            issues.append(f"{name}: {state['detail']}")
    if not os.access("/dev/uinput", os.R_OK | os.W_OK):
        issues.append("No access to /dev/uinput. Install the udev/module rules described in docs/setup.md.")
    import evdev
    from hotkey import COLOR_LETTERS, CTRL, FORMAT_LETTERS, INTERRUPT_LETTERS, LETTERS, SELECT_LETTERS
    required = LETTERS | SELECT_LETTERS | COLOR_LETTERS | FORMAT_LETTERS | INTERRUPT_LETTERS
    keyboards = []
    selected = config.get("hotkey", {}).get("devices")
    for path in selected if selected is not None else evdev.list_devices():
        try:
            with closing(evdev.InputDevice(path)) as device:
                capabilities = device.capabilities()
                keys = set(capabilities.get(evdev.ecodes.EV_KEY, []))
                if (required <= keys and keys & CTRL and not device.name.startswith("Seth shortcut")
                        and (selected is not None or evdev.ecodes.EV_REL not in capabilities)):
                    keyboards.append(path)
        except OSError:
            continue
    if not keyboards or not any(os.access(path, os.R_OK | os.W_OK) for path in keyboards):
        issues.append("No accessible keyboard. Check the keyboard udev rule and hotkey.devices configuration.")
    if issues:
        print("\nAttention needed:")
        for issue in issues:
            print(f"- {issue}")
    else:
        print("\nSeth services and dependencies are ready.")
    return 1 if issues else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="seth", description=__doc__)
    parser.add_argument("--config", type=Path, help="Configuration to install; subsequent commands remember it")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("install", help="Install the command and enable graphical-session autostart")
    commands.add_parser("up", help="Start all Seth services and report readiness (also the default)")
    commands.add_parser("down", help="Stop all Seth services")
    restart = commands.add_parser("restart", help="Restart one component or the whole application")
    restart.add_argument("component", choices=("listener", "browser", "all"), default="listener", nargs="?")
    status = commands.add_parser("status", help="Show service and provider status without changing browser tabs")
    status.add_argument("--json", action="store_true")
    logs = commands.add_parser("logs", help="Follow service logs")
    logs.add_argument("component", choices=("listener", "browser", "all"), default="all", nargs="?")
    logs.add_argument("--no-follow", action="store_true")
    logs.add_argument("-n", "--lines", type=int, default=30)
    commands.add_parser("doctor", help="Check services, permissions and API connectivity; send no inference")
    args = parser.parse_args(argv)
    command = args.command or "up"
    try:
        if not shutil.which("systemctl"):
            raise ValueError("Seth's launch command requires Linux user systemd services.")
        if command == "logs":
            units = COMPONENTS.values() if args.component == "all" else [COMPONENTS[args.component]]
            invocation = ["journalctl", "--user", "--no-pager", "-n", str(args.lines)]
            for unit in units:
                invocation.extend(["-u", unit])
            if not args.no_follow:
                invocation.append("-f")
            return subprocess.call(invocation)
        if command == "down":
            saved = installation()
            config_path = (args.config or Path(saved.get("config", ROOT / "config.json"))).expanduser().resolve()
            require_installation(config_path)
            with command_lock():
                return run_command(command, args, {}, saved)
        missing = missing_dependencies()
        if missing:
            raise ValueError("Missing dependencies: " + ", ".join(missing)
                             + f". Install with {ROOT / '.venv/bin/pip'} install -r {ROOT / 'requirements-browser.txt'}"
                             + f" -r {ROOT / 'requirements-overlay.txt'}")
        saved = installation()
        config_path = (args.config or Path(saved.get("config", ROOT / "config.json"))).expanduser().resolve()
        from settings import load_config
        config = load_config(config_path)
        if command == "install":
            with command_lock():
                return install(config_path)
        if command in ("up", "down", "restart"):
            saved = require_installation(config_path)
        # systemd deduplicates jobs; the lock also serializes up/down and setup metadata.
        with ExitStack() as stack:
            if command in ("up", "down", "restart"):
                stack.enter_context(command_lock())
            return run_command(command, args, config, saved)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        print(f"seth: {error}", file=sys.stderr)
        return 1


def run_command(command, args, config, saved):
    if command == "up":
        import_desktop_environment()
        systemctl("reset-failed", *UNITS, check=False, capture=True)
        started = systemctl("start", *UNITS, check=False)
        if saved.get("restart_listener") and started.returncode == 0:
            systemctl("restart", COMPONENTS["listener"])
            saved["restart_listener"] = False
            write_json(installation_file(), saved)
        print_status(collect_status(config, probe=True))
        return started.returncode
    if command == "down":
        # Finish cancellation before closing the browser used by inference.
        systemctl("stop", COMPONENTS["listener"])
        systemctl("stop", UNITS[0], COMPONENTS["browser"])
        print("Seth services stopped.")
        return 0
    if command == "restart":
        if args.component == "all":
            systemctl("stop", COMPONENTS["listener"])
            systemctl("restart", COMPONENTS["browser"])
            systemctl("start", *UNITS)
        else:
            systemctl("restart", COMPONENTS[args.component])
        print_status(collect_status(config))
        return 0
    if command == "doctor":
        return doctor(config)
    report = collect_status(config)
    print_status(report, as_json=args.json)
    return 0 if all(s.get("ActiveState") == "active" for s in report["services"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
