"""Local runner identity, folder policy and verified executable copies."""

import hashlib
import json
import shutil
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import TypeAdapter

from flow_atelier.schemas.conduit import TaskDefinition, ToolType
from flow_atelier.schemas.runner import Folder, Label, Name, Pair, Poll
from flow_atelier.services.store.filesystem import FilesystemStore


class Local:
    def __init__(self, user_dir: Path):
        self.user_dir = user_dir.resolve()
        self.root = self.user_dir / "runner"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identity (
                    slot INTEGER PRIMARY KEY CHECK(slot=1), origin TEXT, id TEXT, token TEXT);
                CREATE TABLE IF NOT EXISTS folders (
                    id TEXT PRIMARY KEY, label TEXT, path TEXT, dev TEXT, ino TEXT);
                CREATE TABLE IF NOT EXISTS approvals (
                    folder TEXT, name TEXT, digest TEXT, PRIMARY KEY(folder,name));
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

    def poll(self, client, *, claim: bool = True):
        return client.post("poll", Poll(folders=self.inventory(), claim=claim),
                           self.identity()["token"])

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
                if conduit in ancestors or len(ancestors) >= 25:
                    raise ValueError("cyclic or too deeply nested conduit")
                dest = copy / "conduits" / conduit
                if dest.exists():
                    return
                src = source.conduit_dir(conduit)
                if src.resolve() != src:
                    raise ValueError("symlink in conduit path")
                # Hash exactly the copied bytes, including file names and executable bits.
                def copy_file(src_file, dst_file):
                    path = Path(src_file)
                    if not stat.S_ISREG(path.lstat().st_mode):
                        raise ValueError("expected regular conduit file")
                    shutil.copy2(path, dst_file)
                    dest_file = Path(dst_file)
                    key = dest_file.relative_to(copy).as_posix()
                    with dest_file.open("rb") as stream:
                        manifest[key] = (dest_file.stat().st_mode & 0o111,
                                         hashlib.file_digest(stream, "sha256").hexdigest())
                    return dst_file

                def reject_links(directory, entries):
                    if any((Path(directory) / entry).is_symlink() for entry in entries):
                        raise ValueError("symlink in conduit bundle")
                    return []

                shutil.copytree(src, dest, copy_function=copy_file, ignore=reject_links)
                recipe = FilesystemStore(copy).read_conduit(conduit)
                def children(tasks: list[TaskDefinition]) -> None:
                    for task in tasks:
                        if task.tasks:
                            children(task.tasks)
                        elif task.tool == ToolType.conduit:
                            visit(task.task.strip(), (*ancestors, conduit))
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
