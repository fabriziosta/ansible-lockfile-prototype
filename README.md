# What is this?

This repo contains a proof-of-concept tool that generates lockfiles for Ansible
Galaxy collections as expected by [Hermeto]. The goal is to make it possible to
run a container build without network access: this tool resolves collection
dependencies first, then Hermeto (or its generic fetcher) can download the
pinned artifacts offline.

See Hermeto issue
[#1071](https://github.com/hermetoproject/hermeto/issues/1071).

Coding agents: follow [AGENTS.md](AGENTS.md) (keep this README updated when user-facing behavior changes).

**There are no stability guarantees.**

[Hermeto]: https://hermetoproject.github.io/hermeto/

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

For tests (with the venv still active):

```bash
python3 -m pip install -e '.[test]'
```

## How to run

By default the tool discovers `requirements.yml` / `requirements.yaml` and reads
galaxy servers from `ansible.cfg` (same model as
[configuring Automation Hub as primary content source](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html/getting_started_with_automation_hub/configure-hub-primary)):

```bash
# from a project that has ansible.cfg + requirements.yml
ansible-lockfile-prototype --outfile=ansible.lock.yaml
```

```console
$ ansible-lockfile-prototype --help
usage: ansible-lockfile-prototype [-h] [--project-dir PROJECT_DIR]
                                  [--ansible-cfg PATH] [--requirements PATH]
                                  [--outfile OUTFILE]
                                  [--export-generic PATH] [--token-env NAME]
                                  [--prefer-remote] [--print-schema] [--debug]
                                  [input_file]
```

Optional Hermeto generic export:

```bash
ansible-lockfile-prototype \
  --outfile=ansible.lock.yaml \
  --export-generic=artifacts.lock.yaml
```

## Discovery

**Requirements** (first match under `--project-dir`, or the directory of
`ansible.cfg` when one is found):

- `requirements.yml` / `requirements.yaml`
- `collections/requirements.yml` / `collections/requirements.yaml`

Or pass a custom filename explicitly:

```bash
ansible-lockfile-prototype collections/ee-supported-requirements.yml
# equivalent:
ansible-lockfile-prototype --requirements collections/ee-supported-requirements.yml
```

Requirements files list collection names/versions **or** local tarball paths.
They do **not** need a `type: file` marker for vendored archives.

**Servers** come from `ansible.cfg`:

1. `$ANSIBLE_CONFIG` if set
2. `./ansible.cfg` (and parent directories)
3. `~/.ansible.cfg`
4. `/etc/ansible/ansible.cfg`

If no `server_list` is configured, public Galaxy is used.

## Local collection tarballs

Vendored layouts such as
[aap-konflux-vendor-collections](https://github.com/ansible-automation-platform/aap-konflux-vendor-collections)
use path-style `name` entries with **no** `type: file` and **no** explicit
version:

```yaml
---
collections:
  - name: collections/amazon-aws-10.3.0.tar.gz
  - name: collections/kubernetes-core-6.4.0.tar.gz
```

The tool infers a local tarball when `name`/`source` looks like a path (for
example ends with `.tar.gz`) and the file exists on disk. Paths are resolved
relative to the requirements file directory, its parents (up through
`--project-dir`), and `--project-dir` itself—so `ee-supported/requirements-2.7.yml`
finds `collections/*.tar.gz` at the repository root.

Identity and version come from the archive’s `MANIFEST.json`
(`collection_info`); the lockfile entry uses `server: local`, a `file://` URL,
and the tarball’s sha256/size. Transitive dependencies listed in the manifest
are resolved next (locally if also vendored, otherwise via `server_list`).

With **`--prefer-remote`**, the tarball is still required (FQCN/version from
`MANIFEST.json`), but download URL, checksum, size, and server name come from
`server_list`. If the local tarball sha256 differs from the remote artifact, a
warning is logged and the remote values are kept. Missing servers or a missing
remote version fail hard (no `file://` fallback). In this mode
`--export-generic` includes those collections.

`--export-generic` **skips** local (`file://`) collections—they are already in
the source tree.

If a `.tar.gz` path exists but is only a **Git LFS pointer** (common before
`git lfs pull`), the tool errors with install/pull instructions. In the vendor
collections repo:

```bash
sudo dnf install git-lfs   # or: sudo apt install git-lfs
cd /path/to/aap-konflux-vendor-collections
git lfs install
git lfs pull
```

```bash
ansible-lockfile-prototype \
  --project-dir /path/to/aap-konflux-vendor-collections \
  --requirements ee-supported/requirements-2.7.yml \
  --outfile=ansible.lock.yaml
```

```bash
# same vendor tree, but lock remote Galaxy/AH URLs for Hermeto prefetch
ansible-lockfile-prototype \
  --project-dir /path/to/aap-konflux-vendor-collections \
  --requirements ee-supported/requirements-2.7.yml \
  --prefer-remote \
  --outfile=ansible.lock.yaml \
  --export-generic=artifacts.lock.yaml
```

## ansible.cfg and server priority

```ini
[galaxy]
server_list = automation_hub, galaxy

[galaxy_server.automation_hub]
url=https://console.redhat.com/api/automation-hub/content/published/
auth_url=https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token
token=my_ah_token

[galaxy_server.galaxy]
url=https://galaxy.ansible.com
```

`server_list` is a priority list (ansible-galaxy behavior):

1. Ask the first server for the collection/version.
2. If it exists there, stop.
3. Otherwise try the next server, and so on.

Username/password-only servers (private hubs) are skipped with a warning in this
prototype.

Prefer not committing tokens: use `--token-env AUTOMATION_HUB_TOKEN` so the
secret is read from the environment instead of `token=` in the file.

SSO (`auth_url`) uses the refresh-token grant (`client_id` defaults to
`cloud-services`) and calls Hub APIs with `Authorization: Bearer <access_token>`.
Token-only servers use `Authorization: Token <token>`.

## Optional ansible.in.yaml

You can still pass an explicit `ansible.in.yaml` (schema via `--print-schema`)
to list collections and/or `contentOrigin.galaxy` without discovery.

## Output

```yaml
---
lockfileVersion: 1
lockfileVendor: ansible
collections:
  - name: kubernetes.core
    version: "6.1.0"
    url: https://...
    checksum: sha256:...
    size: 12345
    server: automation_hub
auth:
  name: automation_hub
  url: https://console.redhat.com/api/automation-hub/content/published/
  token_from: ansible.cfg   # or token_env: AUTOMATION_HUB_TOKEN
  auth_url: https://sso.redhat.com/.../token
  client_id: cloud-services
```

### Generic export and Hermeto auth

`--export-generic` with credentials emits Hermeto generic lockfile **v2.0** with
per-artifact bearer auth.

- Token-only + `token_env`: `Bearer $<token_env>`
- SSO / Automation Hub: `Bearer $AUTOMATION_HUB_ACCESS_TOKEN`

Hermeto does **not** perform the SSO refresh exchange. For Automation Hub
prefetch via the generic fetcher, supply an **access token** in
`AUTOMATION_HUB_ACCESS_TOKEN` at Hermeto run time. This tool still exchanges
the refresh token itself while generating the lockfile.

## Non-goals (this cut)

- Username/password authentication for private automation hubs
- Roles
- Explicit `type: git`, `type: dir`, or `type: url` requirement sources
- Rewriting `requirements.yml` to local paths after prefetch (Hermeto inject-files territory)
- A Hermeto Ansible package-manager backend

Local path-style `.tar.gz` collection archives (inferred from `name:` without `type:`) **are** supported; see [Local collection tarballs](#local-collection-tarballs).
