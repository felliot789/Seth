"""Host-only Unix socket trigger. Never grabs, observes, or synthesizes keys."""

import hashlib
import os
from pathlib import Path
import socket
import stat
import tempfile


class TriggerError(RuntimeError):
    pass


def read_line(connection):
    data = bytearray()
    while len(data) < 64 and not data.endswith(b"\n"):
        chunk = connection.recv(64 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def socket_path(project_dir):
    directory = Path(tempfile.gettempdir()) / f"seth-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise TriggerError("Host trigger directory must be owned by you with permissions 0700.")
    name = hashlib.sha256(str(Path(project_dir).resolve()).encode()).hexdigest()[:16]
    return directory / (name + ".sock")


class SethTrigger:
    def __init__(self, project_dir, callback):
        self.path = socket_path(project_dir)
        self.callback = callback
        self.server = None

    def start(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
        except OSError:
            server.close()
            raise TriggerError(
                f"Cannot bind host trigger {self.path}. Another listener may be running. "
                "After an unclean shutdown, remove this socket only after confirming the listener stopped."
            ) from None
        self.server = server
        try:
            self.path.chmod(0o600)
            server.listen(4)
            server.settimeout(0.2)
        except Exception:
            self.stop()
            raise

    def serve(self, stop_event):
        if self.server is None:
            raise TriggerError("Start the host trigger before serving requests.")
        while not stop_event.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            with client:
                client.settimeout(0.2)
                try:
                    command = read_line(client)
                    if command != b"cycle\n":
                        client.sendall(b"invalid\n")
                    else:
                        accepted = not stop_event.is_set() and self.callback()
                        client.sendall(b"accepted\n" if accepted else b"busy\n")
                except (OSError, socket.timeout):
                    continue

    def stop(self):
        if self.server is not None:
            self.server.close()
            self.server = None
            self.path.unlink(missing_ok=True)


def send_trigger(project_dir):
    path = socket_path(project_dir)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        try:
            client.connect(str(path))
            client.sendall(b"cycle\n")
            result = read_line(client).decode("ascii").strip()
        except (OSError, UnicodeError):
            raise TriggerError("No responsive host listener. Start main.py --mode listen first.") from None
    if result not in ("accepted", "busy"):
        raise TriggerError("Host listener returned an invalid response.")
    return result
