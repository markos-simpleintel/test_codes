import datetime as _dt
import os
import sys
import threading
from pathlib import Path

from config import LOG_LEVEL


LOG_LEVELS = {
    "QUIET": 0,
    "ERROR": 1,
    "INFO": 2,
}
ACTIVE_LOG_LEVEL = LOG_LEVELS.get(LOG_LEVEL, LOG_LEVELS["INFO"])
_print_lock = threading.Lock()


def log_message(message: str, level: str = "INFO"):
    if LOG_LEVELS.get(level, LOG_LEVELS["INFO"]) <= ACTIVE_LOG_LEVEL:
        with _print_lock:
            print(message, flush=True)


def log_info(message: str):
    log_message(message, "INFO")


def log_error(message: str):
    log_message(message, "ERROR")


class _NullRunLogger:
    path = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _TextTee:
    def __init__(self, original, log_file, lock):
        self._original = original
        self._log_file = log_file
        self._lock = lock
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", "replace")

    def write(self, data):
        with self._lock:
            self._original.write(data)
            self._log_file.write(data)

    def flush(self):
        with self._lock:
            self._original.flush()
            self._log_file.flush()

    def isatty(self):
        return self._original.isatty()

    def fileno(self):
        return self._original.fileno()


class RunLogger:
    def __init__(self, path=None):
        self.path = Path(path) if path else self._default_path()
        self._file = None
        self._lock = threading.Lock()
        self._stdout_fd = None
        self._stderr_fd = None
        self._saved_stdout_fd = None
        self._saved_stderr_fd = None
        self._stdout_pipe_r = None
        self._stderr_pipe_r = None
        self._stdout_thread = None
        self._stderr_thread = None
        self._old_stdout = None
        self._old_stderr = None
        self._using_fd_tee = False
        self._using_text_tee = False

    @staticmethod
    def _default_path():
        override = os.getenv("RUNNER_LOG_FILE", "").strip()
        if override:
            return Path(override).expanduser()

        log_dir = Path(os.getenv("RUNNER_LOG_DIR", "logs")).expanduser()
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return log_dir / f"runner_{stamp}.log"

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._start_fd_tee()
            return
        except Exception:
            self._cleanup_fd_tee()

        self._start_text_tee()

    def _start_fd_tee(self):
        self._stdout_fd = sys.stdout.fileno()
        self._stderr_fd = sys.stderr.fileno()

        self._file = open(self.path, "ab", buffering=0)
        self._saved_stdout_fd = os.dup(self._stdout_fd)
        self._saved_stderr_fd = os.dup(self._stderr_fd)

        stdout_pipe_r, stdout_pipe_w = os.pipe()
        stderr_pipe_r, stderr_pipe_w = os.pipe()
        self._stdout_pipe_r = stdout_pipe_r
        self._stderr_pipe_r = stderr_pipe_r

        os.dup2(stdout_pipe_w, self._stdout_fd)
        os.dup2(stderr_pipe_w, self._stderr_fd)
        os.close(stdout_pipe_w)
        os.close(stderr_pipe_w)

        self._set_line_buffering()

        self._stdout_thread = threading.Thread(
            target=self._pump_fd,
            args=(self._stdout_pipe_r, self._saved_stdout_fd),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._pump_fd,
            args=(self._stderr_pipe_r, self._saved_stderr_fd),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._using_fd_tee = True

    def _start_text_tee(self):
        self._file = open(self.path, "a", encoding="utf-8", buffering=1)
        self._old_stdout = sys.stdout
        self._old_stderr = sys.stderr
        sys.stdout = _TextTee(self._old_stdout, self._file, self._lock)
        sys.stderr = _TextTee(self._old_stderr, self._file, self._lock)
        self._using_text_tee = True

    def _set_line_buffering(self):
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(line_buffering=True)
                except Exception:
                    pass

    def _pump_fd(self, pipe_r, target_fd):
        while True:
            try:
                chunk = os.read(pipe_r, 8192)
            except OSError:
                break

            if not chunk:
                break

            try:
                os.write(target_fd, chunk)
            except OSError:
                pass

            with self._lock:
                try:
                    self._file.write(chunk)
                    self._file.flush()
                except Exception:
                    pass

    def stop(self):
        if self._using_fd_tee:
            self._stop_fd_tee()
        elif self._using_text_tee:
            self._stop_text_tee()

    def _stop_fd_tee(self):
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass

        if self._saved_stdout_fd is not None:
            try:
                os.dup2(self._saved_stdout_fd, self._stdout_fd)
            except OSError:
                pass

        if self._saved_stderr_fd is not None:
            try:
                os.dup2(self._saved_stderr_fd, self._stderr_fd)
            except OSError:
                pass

        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=2.0)

        self._cleanup_fd_tee()
        self._using_fd_tee = False

    def _stop_text_tee(self):
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass

        if self._old_stdout is not None:
            sys.stdout = self._old_stdout
        if self._old_stderr is not None:
            sys.stderr = self._old_stderr

        if self._file is not None:
            self._file.close()
            self._file = None
        self._using_text_tee = False

    def _cleanup_fd_tee(self):
        for fd_name in (
            "_stdout_pipe_r",
            "_stderr_pipe_r",
            "_saved_stdout_fd",
            "_saved_stderr_fd",
        ):
            fd = getattr(self, fd_name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, None)

        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None


def setup_run_logging():
    disabled = os.getenv("RUNNER_DISABLE_FILE_LOG", "").strip().lower()
    if disabled in ("1", "true", "yes", "on"):
        return _NullRunLogger()

    return RunLogger()
