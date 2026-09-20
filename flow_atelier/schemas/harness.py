"""Harness tool grammar shared by tasks, supervisors and CLI options."""

# Agent names are lowercase slugs. Model ids also allow dots, slashes and underscores.
HARNESS_TOOL_PATTERN = r"^harness:[a-z0-9][a-z0-9-]*(?::[A-Za-z0-9][A-Za-z0-9._/-]*)?$"
