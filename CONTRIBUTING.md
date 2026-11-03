# Contributing to flow-atelier

Thanks for your interest in contributing. This document explains how to submit
changes and the legal terms under which contributions are accepted.

## License of contributions

flow-atelier is open source under the [Apache License, Version 2.0](LICENSE).
The Project is also developed as an open-core product: some editions or
companion services may be offered under commercial terms. To keep both models
possible, contributions are accepted under a Contributor License Agreement.

## Contributor License Agreement (CLA)

Before your contribution can be merged, you must sign the
[Contributor License Agreement](CLA.md). The CLA confirms you have the right to
submit your work and grants the Owner the rights needed to distribute it as open
source and, where applicable, under commercial terms. You keep the copyright to
your contributions.

Signing is a one-time, automated step:

1. Open your pull request.
2. The CLA Assistant bot will comment on the PR with a link to the CLA.
3. Reply on the PR with exactly:

   > I have read the CLA Document and I hereby sign the CLA

4. The bot records your signature in `signatures/version1/cla.json`. One
   signature covers all your present and future contributions.

If you are contributing on behalf of a company, contact the Owner to arrange a
Corporate CLA before submitting.

## How to contribute

1. Fork the repository and create a branch from `main`.
2. Make your change. Keep commits focused and write clear messages.
3. Run the test suite and linters locally before opening a PR:
   ```bash
   uv run pytest
   uv run ruff check .
   ```
4. Open a pull request describing what the change does and why.
5. Sign the CLA when the bot prompts you (see above).

## Reporting bugs and requesting features

Open an issue on GitHub. For bugs, include the flow-atelier version, your OS,
the conduit or command you ran, and the full output.

Do not open a public issue for a security problem. See
[SECURITY.md](SECURITY.md) to report it privately.
