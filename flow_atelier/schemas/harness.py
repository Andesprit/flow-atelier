"""Harness tool grammar shared by tasks, supervisors and CLI options."""

# Model ids include provider paths and context-window suffixes such as Claude's opus[1m].
HARNESS_TOOL_PATTERN = r"^harness:[a-z0-9][a-z0-9-]*(?::[A-Za-z0-9][A-Za-z0-9._/\[\]-]*)?$"
