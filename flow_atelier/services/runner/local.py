"""Local runner identity, folder policy and verified executable copies."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import TypeAdapter

from flow_atelier.schemas.conduit import MAX_NESTED_CONDUIT_DEPTH, TaskDefinition, ToolType
from flow_atelier.schemas.runner import Folder, Label, Name, Pair, Poll
from flow_atelier.services.runner.privacy import outbound
from flow_atelier.services.store.filesystem import FilesystemStore


def _check_shell_entry(command: str, bundle: Path) -> None:
    """A general shell program cannot be inspected for all files it may execute."""
    command = command.strip()
    if re.fullmatch(r"""(?:true|false|pwd|echo|printf)(?:[ \t]+[A-Za-z0-9_ ./:'"=,+%>@-]+)?""",
                    command):
        return
    entry = re.fullmatch(
        r"""(?:(?:bash|sh|python|python3|node)[ \t]+)?(['"]?)"""
        r"""\{\{conduit_dir\}\}/([A-Za-z0-9_./-]+)\1(?:[ \t]+[A-Za-z0-9_.=-]+)*""",
        command,
    )
    if entry:
        relative = Path(entry[2])
        if (not relative.is_absolute() and ".." not in relative.parts
                and (bundle / relative).is_file()):
            return
    raise ValueError("use a bundled helper under {{conduit_dir}} for this shell task")


class Local:
    def __init__(self, user_dir: Path):
        self.user_dir = user_dir.resolve()
        self.root = self.user_dir / "runner"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("runner state must be a local directory")
        if os.name == "posix":
            self.root.chmod(0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identity (
                    slot INTEGER PRIMARY KEY CHECK(slot=1), origin TEXT, id TEXT, token TEXT);
                CREATE TABLE IF NOT EXISTS folders (
                    id TEXT PRIMARY KEY, label TEXT, path TEXT, dev TEXT, ino TEXT);
                CREATE TABLE IF NOT EXISTS approvals (
                    folder TEXT, name TEXT, digest TEXT, PRIMARY KEY(folder,name));
                CREATE TABLE IF NOT EXISTS heartbeat (
                    slot INTEGER PRIMARY KEY CHECK(slot=1), last_ack REAL, refused INTEGER);
            """)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.root / "runner.sqlite3")) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def pair(self, client, token: str) -> None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM identity").fetchone():
                raise ValueError("runner already paired")
            identity = client.post("pair", Pair(), token)
            db.execute("INSERT INTO identity VALUES (1,?,?,?)",
                       (client.origin, identity.id, identity.token.get_secret_value()))

    def identity(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM identity").fetchone()
        if row is None:
            raise ValueError("pair this runner first")
        return row

    def expose(self, folder: Path, label: str) -> str:
        TypeAdapter(Label).validate_python(label)
        path = folder.resolve(strict=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("expected a directory")
        fid = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO folders VALUES (?,?,?,?,?)",
                       (fid, label, str(path), str(info.st_dev), str(info.st_ino)))
        return fid

    def folder_path(self, fid: str) -> Path:
        with self.db() as db:
            row = db.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
        if row is None:
            raise ValueError("unknown runner folder")
        path = Path(row["path"])
        info = path.stat()
        if (path != path.resolve(strict=True)
                or (str(info.st_dev), str(info.st_ino)) != (row["dev"], row["ino"])):
            raise ValueError("registered directory changed")
        return path

    def inventory(self) -> list[Folder]:
        with self.db() as db:
            return [Folder(id=fid, label=label, conduits=[
                row[0] for row in db.execute(
                    "SELECT name FROM approvals WHERE folder=? ORDER BY name", (fid,))])
                for fid, label in db.execute("SELECT id,label FROM folders ORDER BY id")]

    def send(self, client, route, message):
        identity = self.identity()
        if client.origin != identity["origin"]:
            raise ValueError("runner credential belongs to another origin")
        message = outbound(message, self.root, identity["token"])
        try:
            return client.post(route, message, identity["token"])
        except PermissionError:
            with self.db() as db:
                db.execute("UPDATE heartbeat SET refused=1")
            raise

    def poll(self, client, *, claim: bool = True):
        reply = self.send(client, "poll", Poll(folders=self.inventory(), claim=claim))
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO heartbeat VALUES (1,?,0)", (time.time(),))
        return reply

    def status(self) -> tuple[bool, float | None]:
        with self.db() as db:
            row = db.execute("SELECT last_ack,refused FROM heartbeat").fetchone()
        if row is None:
            return False, None
        return not row["refused"] and time.time() - row["last_ack"] < 60, row["last_ack"]

    @contextmanager
    def snapshot(self, fid: str, name: str, *, approving: bool = False):
        project = self.folder_path(fid)
        if not approving:
            with self.db() as db:
                row = db.execute("SELECT digest FROM approvals WHERE folder=? AND name=?",
                                 (fid, name)).fetchone()
            if row is None:
                raise ValueError("conduit requires local approval")
        source = FilesystemStore(project / ".atelier", self.user_dir)
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            copy = Path(tmp)
            manifest = {}

            def visit(conduit: str, ancestors: tuple[str, ...]) -> None:
                TypeAdapter(Name).validate_python(conduit)
                if conduit in ancestors or len(ancestors) >= MAX_NESTED_CONDUIT_DEPTH:
                    raise ValueError("cyclic or too deeply nested conduit")
                dest = copy / "conduits" / conduit
                if dest.exists():
                    return
                src = source.conduit_dir(conduit)
                if src.resolve() != src:
                    raise ValueError("symlink in conduit path")
                # Preserve links while copying so validation never follows them.
                shutil.copytree(src, dest, symlinks=True)
                for path in dest.rglob("*"):
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode):
                        raise ValueError("symlink in conduit bundle")
                    if stat.S_ISDIR(mode):
                        continue
                    if not stat.S_ISREG(mode):
                        raise ValueError("expected regular conduit file")
                    key = path.relative_to(copy).as_posix()
                    with path.open("rb") as stream:
                        manifest[key] = (mode & 0o111,
                                         hashlib.file_digest(stream, "sha256").hexdigest())
                recipe = FilesystemStore(copy).read_conduit(conduit)

                def children(tasks: list[TaskDefinition]) -> None:
                    for task in tasks:
                        if task.tasks:
                            children(task.tasks)
                        elif task.tool == ToolType.conduit:
                            visit(task.task.strip(), (*ancestors, conduit))
                        elif task.tool == ToolType.bash:
                            _check_shell_entry(task.task, dest)
                children(recipe.tasks)

            visit(name, ())
            digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
            with self.db() as db:
                if approving:
                    db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?)",
                               (fid, name, digest))
                else:
                    row = db.execute("SELECT digest FROM approvals WHERE folder=? AND name=?",
                                     (fid, name)).fetchone()
                    if row is None or row[0] != digest:
                        raise ValueError("conduit requires local approval")
            yield project, copy

    def approve(self, fid: str, name: str) -> None:
        with self.snapshot(fid, name, approving=True):
            pass
