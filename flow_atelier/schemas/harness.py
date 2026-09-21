"""Harness tool grammar shared by tasks, supervisors and CLI options."""

# `harness:<name>[:<model>[:<effort>]]`.
#
# Model ids include provider paths and context-window suffixes such as
# Claude's opus[1m]. Effort is whatever the *chosen model* advertises under
# the ACP `thought_level` config category, so the grammar only fixes its
# shape — the agent decides which values exist, and an unknown one fails at
# session setup with the list the agent does offer.
#
# Effort is reachable only behind an explicit model: the two are selected in
# that order on the session, and the choices an agent offers for one model
# are not the choices it offers for another.
HARNESS_TOOL_PATTERN = (
    r"^harness:[a-z0-9][a-z0-9-]*"
    r"(?::[A-Za-z0-9][A-Za-z0-9._/\[\]-]*"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?)?$"
)
