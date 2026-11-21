"""Closed wire contracts for the outbound runner v1 API."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Id = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$", min_length=1, max_length=100)]
Label = Annotated[str, Field(pattern=r"^[A-Za-z0-9 _-]+$", min_length=1, max_length=80)]
Seq = Annotated[int, Field(ge=0)]


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Pair(Wire):
    pass


class Identity(Wire):
    id: Id
    token: SecretStr


class Folder(Wire):
    id: Id
    label: Label
    conduits: list[Name]


class Poll(Wire):
    folders: list[Folder]
    claim: bool


class Job(Wire):
    id: Id
    folder: Id
    conduit: Name


class Dispatch(Wire):
    job: Job | None
    after: Seq


class Result(Wire):
    job: Id
    status: Literal["started", "completed", "failed", "refused", "unknown"]


class Record(Wire):
    sequence: Annotated[int, Field(ge=1)]
    job: Id
    folder: Id
    kind: Literal["event", "log", "chat"]
    status: Literal["started", "completed", "failed", "skipped", "cancelled", "delivered"]
    task: Name | None = None


class Records(Wire):
    records: Annotated[list[Record], Field(min_length=1, max_length=100)]


class Ack(Wire):
    pass


ENDPOINTS = {"pair": (Pair, Identity), "poll": (Poll, Dispatch),
             "result": (Result, Ack), "records": (Records, Ack)}


def messages() -> dict:
    from pydantic.json_schema import models_json_schema

    models = [(model, "validation") for pair in ENDPOINTS.values() for model in pair]
    refs, schema = models_json_schema(models)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            **schema, "type": "object", "additionalProperties": False,
            "properties": {model.__name__: refs[model, mode] for model, mode in models}}
