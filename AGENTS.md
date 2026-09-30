# Agent instructions

This repository’s instructions for humans and coding agents are in:

- [README.md](README.md) — purpose, install, CLI, ansible.cfg / local tarball behavior, non-goals

Read the README before editing. Prefer existing package layout under `ansible_lockfile/` and tests under `tests/unit/`.

## Keep the README current

**Always update [README.md](README.md) in the same change** when behavior, CLI flags, inputs, outputs, install steps, or supported/non-goals change. Do not leave the README stale across commits.

Examples that require a README update:

- New or changed CLI options / discovery rules
- New auth, server, or local-tarball behavior
- Changes to lockfile or `--export-generic` format
- Install / venv / Git LFS prerequisites
- Moving items into or out of “Non-goals”

If a commit only refactors internals with no user-visible change, a README edit is not required.

## Project shape

| Path | Role |
|------|------|
| `ansible_lockfile/` | CLI and library (`galaxy`, `ansible_cfg`, `local_collection`, `export_generic`, …) |
| `tests/unit/` | Unit tests (mocked Galaxy/SSO; synthetic local tarballs) |
| `pyproject.toml` | Package metadata and `ansible-lockfile-prototype` console script |

## Conventions

- Hermeto-facing PoC: fully resolved lockfile (URL + checksum + size); no `ansible-galaxy` subprocess for resolution.
- Secrets: never commit tokens; use `token=` in ansible.cfg only as a local convenience, prefer `--token-env`.
- Local vendored collections: infer from path-like `name:` ending in `.tar.gz` (no `type: file` required); read version from `MANIFEST.json`; detect Git LFS pointers and error with pull instructions.
- After substantive code changes, run: `python -m pytest` (from a venv with `pip install -e '.[test]'`).

## Do not

- Add parallel instruction trees (e.g. `.cursor/rules`) unless the user asks; keep agent guidance in this file and the README.
- Expand scope into Hermeto package-manager implementation unless explicitly requested.
