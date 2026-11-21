"""Private login files used by sandboxed agents."""

import json
import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

from flow_atelier.services.store.events import file_lock


def replace_private(path: Path, data: bytes) -> None:
    with NamedTemporaryFile(dir=path.parent, delete_on_close=False) as temp:
        temp.write(data)
        temp.close()
        os.replace(temp.name, path)


def write_back_login(source: Path, real: Path, original: bytes, refreshed: bytes | None) -> None:
    """Commit a refreshed login only while the original login is still current."""
    if refreshed is None or refreshed == original:
        return
    if not isinstance(json.loads(refreshed), dict):
        raise ValueError("Invalid refreshed agent credentials")
    # Logging out may also remove the directory containing the lock.
    with suppress(FileNotFoundError), file_lock(real.with_suffix(".atelier.lock"), wait=True):
        if source.resolve() == real and real.is_file() and real.read_bytes() == original:
            replace_private(real, refreshed)
