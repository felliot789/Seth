"""Host-only Unix socket trigger. Never grabs, observes, or synthesizes keys."""

import hashlib
import errno
import fcntl
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
        self._lock_fd = None
        self._socket_identity = None

    def start(self):
        if self.server is not None or self._lock_fd is not None:
            raise TriggerError("This host listener is already started.")
        lock_path = self.path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise TriggerError("Host listener lock must be a regular file owned by you.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise TriggerError("Another Seth listener is already running for this project.") from None
        except Exception:
            os.close(fd)
            raise
        self._lock_fd = fd
        try:
            self._remove_stale_socket()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(self.path))
            except Exception:
                server.close()
                raise
            self.server = server
            info = self.path.lstat()
            self._socket_identity = (info.st_dev, info.st_ino)
            self.path.chmod(0o600)
            server.listen(4)
            server.settimeout(0.2)
        except Exception:
            self.stop()
            raise

    def _remove_stale_socket(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise TriggerError("Refusing to replace a host trigger path that is not your socket.")
        # The process lock prevents competing new listeners. A connect-only probe
        # also protects listeners from older versions, without requesting a cycle.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except OSError as error:
                if error.errno == errno.ENOENT:
                    return
                if error.errno != errno.ECONNREFUSED:
                    raise TriggerError("Host trigger is in use or cannot be checked; refusing to replace it.") from None
            else:
                raise TriggerError("Another Seth listener is already running for this project.")
        current = self.path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise TriggerError("Host trigger changed during startup; try again.")
        self.path.unlink()

    def serve(self, stop_event, poll=None):
        if self.server is None:
            raise TriggerError("Start the host trigger before serving requests.")
        if poll is not None:
            self.server.settimeout(0.01)
        while not stop_event.is_set():
            if poll is not None:
                poll()
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
        try:
            if self.server is not None:
                self.server.close()
                self.server = None
                try:
                    info = self.path.lstat()
                    if (info.st_dev, info.st_ino) == self._socket_identity:
                        self.path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            self._socket_identity = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            # Keep the lock inode: unlinking it could let two processes lock
            # different files under the same name after a shutdown/restart race.


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
