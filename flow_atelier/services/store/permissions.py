"""Owner-only permissions for run history on POSIX."""

import os
import stat
from pathlib import Path


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        mode = path.stat(follow_symlinks=False).st_mode
        if not stat.S_ISDIR(mode):
            raise NotADirectoryError(path)
        if stat.S_IMODE(mode) != 0o700:
            path.chmod(0o700, follow_symlinks=False)


def private_opener(path: str, flags: int) -> int:
    if os.name != "posix":
        return os.open(path, flags, 0o600)
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_private_text(path: Path, text: str, *, encoding: str | None = None) -> None:
    with open(path, "w", encoding=encoding, opener=private_opener) as stream:
        stream.write(text)
