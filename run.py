import asyncio
import atexit
import errno
import logging
import os
import socket
from dotenv import load_dotenv

load_dotenv(encoding='utf-8')

from bot import start_bot
from bot.misc import EnvKeys

_PIDFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", ".bot.pid")


def _acquire_pidfile() -> None:
    """Fail closed when another instance holds the pidfile (same-token double
    polling causes 409 flapping, split orders and duplicated broadcasts)."""
    os.makedirs(os.path.dirname(_PIDFILE), exist_ok=True)
    try:
        fd = os.open(_PIDFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        try:
            with open(_PIDFILE) as fh:
                old_pid = int(fh.read().strip())
        except (OSError, ValueError):
            old_pid = None
        alive = False
        if old_pid:
            try:
                os.kill(old_pid, 0)
                alive = True
            except (OSError, OverflowError):
                alive = False
        if alive:
            print(f"Refusing to start: another bot instance holds {_PIDFILE} (pid {old_pid}).")
            raise SystemExit(1)
        os.unlink(_PIDFILE)
        return _acquire_pidfile()
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    atexit.register(_release_pidfile)


def _release_pidfile() -> None:
    try:
        with open(_PIDFILE) as fh:
            if fh.read().strip() == str(os.getpid()):
                os.unlink(_PIDFILE)
    except OSError:
        pass


def admin_port_is_busy() -> bool:
    """Return whether another local process is already serving the admin port."""
    host = str(EnvKeys.ADMIN_HOST or "localhost").strip().lower()
    probe_host = "127.0.0.1" if host in {"", "localhost", "0.0.0.0", "::"} else host

    try:
        with socket.create_connection(
            (probe_host, EnvKeys.ADMIN_PORT), timeout=0.25
        ):
            return True
    except OSError:
        return False

if __name__ == "__main__":
    _acquire_pidfile()
    if admin_port_is_busy():
        print(
            f"My Store уже запущен: порт {EnvKeys.ADMIN_PORT} занят. "
            f"Откройте http://127.0.0.1:{EnvKeys.ADMIN_PORT}/admin "
            "или остановите предыдущий процесс перед перезапуском."
        )
        raise SystemExit(1)

    try:
        asyncio.run(start_bot())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
