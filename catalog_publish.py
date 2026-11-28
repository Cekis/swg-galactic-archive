"""Lock and publish generated SQLite catalogs."""

from contextlib import contextmanager
import errno
import os
import shutil
import time


READY_FOR = "ready-for"
RETRY_DELAYS = (0.25, 0.5, 1, 2, 3, 3)
WINDOWS_RETRY_ERRORS = {5, 32, 33}


class CatalogBuildError(OSError):
    pass


@contextmanager
def build_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise CatalogBuildError(f"Another Archive build is using {path}. Try again after it finishes.") from error
            raise
        yield


def publish_catalog(candidate, destination):
    for attempt, delay in enumerate((*RETRY_DELAYS, None)):
        try:
            candidate.replace(destination)
            return
        except OSError as error:
            windows_retry = getattr(error, "winerror", None) in WINDOWS_RETRY_ERRORS
            if delay is None or not windows_retry:
                reason = error.strerror or str(error)
                hint = " Close any program using the catalog or check its write permissions." if windows_retry else ""
                raise CatalogBuildError(
                    f"Could not replace {destination} ({reason}).{hint} "
                    f"The completed catalog is saved at {candidate}. "
                    "Rerun the build with the same options and --if-needed to publish it."
                ) from error
            if attempt == 0:
                print("Windows blocked the catalog replacement. Retrying briefly.", flush=True)
            time.sleep(delay)


def staged_catalogs(destination):
    result = []
    for marker in destination.parent.glob(f"index-*/{READY_FOR}"):
        try:
            work = marker.parent
            if work.resolve() != work or marker.is_symlink() or marker.read_text(encoding="utf-8") != destination.name:
                continue
            candidate = work / "catalog.sqlite3"
            if not candidate.is_symlink() and candidate.is_file():
                result.append((candidate.stat().st_mtime_ns, candidate))
        except (OSError, UnicodeError):
            continue
    return sorted(result, reverse=True)


def discard_stage(destination, candidate):
    work = candidate.parent
    if work.parent == destination.parent and work.name.startswith("index-") and work.resolve() == work:
        shutil.rmtree(work, ignore_errors=True)
