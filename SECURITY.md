# Security policy

## Supported versions

Security fixes go into the latest minor release only. If you run an older
version, upgrade first and check that the problem is still there.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately on GitHub with the
[private report form](https://github.com/Andesprit/flow-atelier/security/advisories/new),
which is also the **Report a vulnerability** button on the repository's
[Security tab](https://github.com/Andesprit/flow-atelier/security). The report
stays private between you and the maintainers.

Please include:

- the flow-atelier version (`atelier --version`), your OS, and how you
  installed it (prebuilt binary or `uv tool install`);
- what an attacker can do, and what they need first, for example a web page
  you visit or access to your network;
- the steps to reproduce it, or a small proof of concept;
- any fix or workaround you know of.

We aim to reply within 7 days. This is a small project, so a fix can take
longer than that.

## Scope

Flow Atelier runs shell commands and AI agents on the machine it runs on. That
is what it is for. So these are not vulnerabilities:

- a conduit, or a conduit package you installed, runs commands or changes
  files;
- an agent does what its prompt and its own settings allow, settings and hooks
  that a checked-out project ships (such as its `.claude/settings.json`)
  included: a `permissions` level does not stop them (see the guide's
  [Security section](docs/guide.md#security));
- someone who can already run `atelier` on your machine, or who has your
  `ATELIER_API_TOKEN`, uses it.

These are, and we want to hear about them:

- a web page you visit, including one on another local port, can drive the
  API on loopback, for example by getting past the Origin, Host or WebSocket
  origin checks;
- a `.env` file in the folder you run in can choose a command Atelier runs;
- the API can be reached from the network without `ATELIER_API_TOKEN`;
- the installer or `atelier self-update` installs a binary whose checksum does
  not match the release.

The [Security section of the guide](docs/guide.md#security) explains how the
API is protected.
