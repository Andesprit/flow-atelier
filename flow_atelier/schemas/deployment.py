"""User-selected deployment destinations and artifacts for idea-to-product."""

from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeploymentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["cloud-run"]
    project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]+(?:-[a-z0-9]+)+$")
    service: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,47}[a-z0-9])?$")
    deploy_account: str = Field(pattern=r"^[a-z][a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$")
    sources: list[str] = Field(min_length=1)
    report_url: str
    report_secret: str = Field(pattern=r"^[A-Za-z0-9_-]+:[1-9][0-9]*$")

    @model_validator(mode="after")
    def validate_destination(self) -> Self:
        import re

        url = urlsplit(self.report_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or "," in self.report_url
            or not re.fullmatch(r"/hooks/errors/[A-Za-z0-9_-]+", url.path)
        ):
            raise ValueError("expected an HTTPS relay URL ending in /hooks/errors/<source>")
        if not self.deploy_account.endswith("@" + self.project + ".iam.gserviceaccount.com"):
            raise ValueError("select a dedicated deployment identity in the target project")
        for source in self.sources:
            path = Path(source)
            if not source or path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError(
                    "select relative release files or directories in the target project"
                )
        return self
