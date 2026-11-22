"""Owner-only permissions for run history on POSIX."""

import os
import stat
from pathlib import Path


def private_dir(path: Path) -> None:
    if os.name != "posix":
        path.mkdir(exist_ok=True)
        return
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        created = False
    else:
        created = True
    mode = path.stat(follow_symlinks=False).st_mode
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError(path)
    current = stat.S_IMODE(mode)
    # An existing read-only history directory must stay read-only.
    owner_mode = 0o700 if created else current & 0o700
    if current != owner_mode:
        try:
            path.chmod(owner_mode, follow_symlinks=False)
        except NotImplementedError:
            # Older libc versions cannot chmod a path without following symlinks.
            try:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except PermissionError:
                # The owner can chmod a directory even without read access.
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise NotADirectoryError(path) from None
                os.chmod(path, owner_mode)
            else:
                try:
                    os.fchmod(fd, owner_mode)
                finally:
                    os.close(fd)


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
