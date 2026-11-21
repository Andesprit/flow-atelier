import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.services.store.events import project_folder


def recipe(atelier, name, task, tool):
    atelier.store.write_conduit(Conduit.model_validate(dict(name=name, description=name, tasks=[
        dict(name="step", task=task, description="step", tool=tool, depends_on=[])
    ])))


async def test_two_resumes_of_interrupted_nested_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    atlas = Atelier()
    recipe(atlas, "inner",
           "if [ ! -f ready ]; then touch started; sleep 5; else echo once >> calls; fi",
           "tool:bash")
    recipe(atlas, "outer", "inner", "tool:conduit")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    code = "import asyncio; from flow_atelier.core.atelier import Atelier; asyncio.run(Atelier().run_conduit('outer', {}))"
    owner = await asyncio.create_subprocess_exec(sys.executable, "-c", code, env=env)
    try:
        async with asyncio.timeout(10):
            while not (tmp_path / "started").exists():
                await asyncio.sleep(0.02)
        [root] = atlas.list_flows()
        [child] = atlas.store.list_child_flows(root)
    finally:
        owner.kill()
        await owner.wait()
    started = [root, child]
    folder = atlas.settings.global_atelier_dir / "projects" / project_folder(tmp_path)
    original = {run: (folder / f"{run}.jsonl").read_bytes() for run in started}
    assert atlas.store.parent_flow_id(child) == root
    assert not (tmp_path / ".atelier/flows").exists()
    (tmp_path / "ready").touch()

    script = "import asyncio,sys; from flow_atelier.core.atelier import Atelier; asyncio.run(Atelier().resume_flow(sys.argv[1]))"
    running = await asyncio.gather(*(asyncio.create_subprocess_exec(
        sys.executable, "-c", script, root, env=env,
    ) for _ in range(2)))
    assert sorted(await asyncio.gather(*(p.wait() for p in running))) == [0, 1]
    assert (tmp_path / "calls").read_text().splitlines() == ["once"]
    assert atlas.store.list_child_flows(root) == [child]
    for run in started:
        assert (folder / f"{run}.jsonl").read_bytes().startswith(original[run])
        assert atlas.get_status(run).status == "completed"


async def test_repair_tail_and_refuse_middle_damage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    atlas = Atelier()
    recipe(atlas, "hello", "echo OK", "tool:bash")
    run = await atlas.run_conduit("hello", {})
    journal = atlas.store.journal(run)
    good = journal.path.read_bytes()
    journal.path.write_bytes(good + b'{"type":"interrupted')
    assert atlas.get_status(run).status == "completed"
    atlas.store.append_input(run, "answer", 42)
    assert atlas.store.read_input(run)["answer"] == 42
    assert journal.path.read_bytes().startswith(good)
    script = (
        "import sys; from flow_atelier.core.atelier import Atelier; s = Atelier().store; "
        "[s.append_input(sys.argv[1], sys.argv[2] + str(i), i) for i in range(10)]"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    writers = await asyncio.gather(*(asyncio.create_subprocess_exec(
        sys.executable, "-c", script, run, f"worker-{i}/", env=env,
    ) for i in range(3)))
    assert await asyncio.gather(*(writer.wait() for writer in writers)) == [0, 0, 0]
    assert atlas.store.read_input(run) == {
        "answer": 42, **{f"worker-{w}/{i}": i for w in range(3) for i in range(10)}
    }
    output = "é" * 50000 + "\r"
    atlas.store.write_outputs(run, {"step": output})
    last = json.loads(journal.path.read_bytes().splitlines()[-1])
    assert (journal.side / last["data"]["files"]["step"]).read_bytes() == output.encode()
    assert atlas.get_outputs(run) == {"step": output}
    first, rest = journal.path.read_bytes().split(b"\n", 1)
    journal.path.write_bytes(first + b"\nBROKEN\n" + rest)
    with pytest.raises(ValueError, match="line 2"):
        atlas.get_status(run)
