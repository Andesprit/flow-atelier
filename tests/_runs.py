"""Inspect a subprocess's run history and build pre-event fixtures."""
import json
import os

import yaml

from flow_atelier.services.store.filesystem import FilesystemStore


def project_store(project, env=None):
    return FilesystemStore(project / ".atelier", (env or os.environ)["ATELIER_GLOBAL_ATELIER_DIR"])


def saved_bytes(journal):
    return {p.relative_to(journal.path.parent): p.read_bytes()
            for p in (journal.path, *journal.side.rglob("*")) if p.is_file()}


def legacy_record(store, flow_id):
    """Re-create a single pre-migration fixture for tests that damage individual files."""
    journal = store.journal(flow_id)
    final = store.base_dir / "flows" / flow_id
    directory = final.with_name("legacy-fixture")
    directory.mkdir(parents=True)
    for name, data in (("input", store.read_input(flow_id)), ("outputs", store.read_outputs(flow_id)),
                       ("sessions", store.read_sessions(flow_id))):
        (directory / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (directory / "progress.json").write_text(store.read_progress(flow_id).model_dump_json())
    (directory / "logs.jsonl").write_text("".join(
        json.dumps(log.model_dump(mode="json")) + "\n" for log in store.read_logs(flow_id)
    ))
    if (journal.side / "steps.jsonl").exists():
        (directory / "steps.jsonl").write_bytes((journal.side / "steps.jsonl").read_bytes())
    journal.path.unlink()
    directory.rename(final)
    return final
