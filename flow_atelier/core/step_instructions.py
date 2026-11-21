"""Read the rule files and skills selected in user settings for harness prompts."""
from pathlib import Path

from pydantic import BaseModel, Field


class StepInstructions(BaseModel):
    project_rules: dict[str, list[str]] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)

    def render(self, project: Path) -> str:
        project = project.resolve()
        files = []
        for name in self.project_rules.get(str(project), []):
            path = (project / name).resolve()
            if Path(name).is_absolute() or not path.is_relative_to(project):
                raise ValueError(f"rule {name!r} is outside the target project")
            files.append(("rule", name, path))
        for name in self.skills:
            if not name or name in {".", ".."} or "/" in name or "\\" in name or ":" in name:
                raise ValueError(f"invalid skill name: {name!r}")
            files.append(("skill", name, Path.home() / ".claude" / "skills" / name / "SKILL.md"))

        sections = []
        for kind, name, path in files:
            try:
                path = path.resolve(strict=True)
                if not path.is_file():
                    raise ValueError("expected a regular file")
                content = path.read_text(encoding="utf-8")
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot read selected {kind} {name!r}: {exc}") from exc
            sections.append(f"Selected {kind} {name} ({path})\n{content}\n\n")
        return "".join(sections)

