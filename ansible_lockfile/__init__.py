from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

from . import ansible_cfg, discover, export_generic, galaxy, schema
from .galaxy import GalaxyServer
from .logging_config import configure_logging
from .models import CollectionItem

logger = logging.getLogger(__name__)

DEFAULT_OUTFILE = "ansible.lock.yaml"


def _build_lockfile(
    collections: list[CollectionItem],
    *,
    servers_used: list[GalaxyServer] | None = None,
) -> dict:
    data: dict = {
        "lockfileVersion": 1,
        "lockfileVendor": "ansible",
        "collections": [c.as_dict() for c in collections],
    }
    if servers_used:
        auth_entries = []
        for server in servers_used:
            meta = server.auth_metadata()
            if meta:
                auth_entries.append(meta)
        if auth_entries:
            data["auth"] = auth_entries if len(auth_entries) > 1 else auth_entries[0]
    return data


def write_lockfile(
    collections: list[CollectionItem],
    path: Path,
    *,
    servers_used: list[GalaxyServer] | None = None,
) -> None:
    data = _build_lockfile(collections, servers_used=servers_used)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            data,
            fh,
            default_flow_style=False,
            sort_keys=False,
            explicit_start=True,
        )


def load_ansible_in(
    path: Path,
    *,
    token_env_override: str | None = None,
) -> tuple[list[dict], list[GalaxyServer]]:
    """Load optional ansible.in.yaml override (collections + optional contentOrigin)."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a YAML mapping")

    schema.validate(data)

    requirements: list[dict] = []
    if data.get("requirementsFile"):
        req_path = Path(data["requirementsFile"])
        if not req_path.is_absolute():
            req_path = path.parent / req_path
        requirements.extend(
            galaxy.load_requirements_file(req_path, project_dir=path.parent)
        )

    for entry in data.get("collections") or []:
        item = {"name": entry["name"]}
        if "version" in entry and entry["version"] is not None:
            item["version"] = str(entry["version"])
        requirements.append(item)

    if not requirements:
        raise SystemExit(f"{path}: no collections to resolve")

    origin = data.get("contentOrigin") or {}
    servers_cfg = origin.get("galaxy") or []
    if servers_cfg:
        servers = [
            galaxy.server_from_mapping(entry, token_env_override=token_env_override)
            for entry in servers_cfg
        ]
    else:
        servers = [GalaxyServer(token_env=token_env_override)]
    return requirements, servers


def resolve_from_project(
    *,
    project_dir: Path,
    requirements_path: Path | None = None,
    ansible_cfg_path: Path | None = None,
    token_env_override: str | None = None,
) -> tuple[list[dict], list[GalaxyServer]]:
    """Discover requirements + ansible.cfg and return requirements and servers."""
    explicit_cfg = ansible_cfg_path is not None
    cfg_path = ansible_cfg_path or ansible_cfg.find_ansible_cfg(project_dir)
    root = discover.project_dir_for(cfg_path, cwd=project_dir)

    if cfg_path is None:
        logger.info(
            "No ansible.cfg found (searched $ANSIBLE_CONFIG, %s and parents, "
            "~/.ansible.cfg, /etc/ansible/ansible.cfg)",
            project_dir.resolve(),
        )
        logger.warning(
            "Defaulting to public Galaxy (%s)",
            galaxy.DEFAULT_GALAXY_URL,
        )
        servers = [
            GalaxyServer(
                name="galaxy",
                url=galaxy.DEFAULT_GALAXY_URL,
                token_env=token_env_override,
            )
        ]
    else:
        if explicit_cfg:
            how = "via --ansible-cfg"
        elif os.environ.get("ANSIBLE_CONFIG"):
            how = "via $ANSIBLE_CONFIG"
        else:
            how = "via discovery"
        logger.info("Using ansible.cfg %s (%s)", cfg_path.resolve(), how)
        servers = ansible_cfg.parse_ansible_cfg(
            cfg_path, token_env_override=token_env_override
        )

    if requirements_path is None:
        requirements_path = discover.find_requirements_file(root)
    requirements = galaxy.load_requirements_file(
        requirements_path, project_dir=root
    )

    return requirements, servers


def _is_ansible_in_file(path: Path) -> bool:
    """True if path is an ansible.in.yaml-style override (not a Galaxy requirements file)."""
    name = path.name.lower()
    if name in {"ansible.in.yaml", "ansible.in.yml"}:
        return True
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return False
    # Galaxy requirements files only use collections/roles; ansible.in adds our keys.
    return "contentOrigin" in data or "requirementsFile" in data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ansible-lockfile-prototype",
        description=(
            "Resolve Ansible collection requirements into a Hermeto-oriented lockfile. "
            "By default discovers requirements.yml/yaml and reads galaxy servers from ansible.cfg."
        ),
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default=None,
        metavar="FILE",
        help=(
            "Optional path to a requirements.yml/yaml (custom name allowed) or an "
            "ansible.in.yaml override. If omitted, discover requirements under --project-dir."
        ),
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Project directory to search for ansible.cfg and requirements files",
    )
    parser.add_argument(
        "--ansible-cfg",
        metavar="PATH",
        help="Explicit ansible.cfg path (skips discovery)",
    )
    parser.add_argument(
        "--requirements",
        metavar="PATH",
        help=(
            "Explicit requirements.yml/yaml path (custom filename allowed; "
            "skips discovery). Overrides a requirements FILE positional if both are set."
        ),
    )
    parser.add_argument(
        "--outfile",
        default=DEFAULT_OUTFILE,
        help=f"Write ansible lockfile here (default: {DEFAULT_OUTFILE})",
    )
    parser.add_argument(
        "--export-generic",
        metavar="PATH",
        help="Also write a Hermeto generic artifacts.lock.yaml to PATH",
    )
    parser.add_argument(
        "--token-env",
        metavar="NAME",
        help=(
            "Read the galaxy/Automation Hub token from this environment variable "
            "(overrides token= in ansible.cfg)"
        ),
    )
    parser.add_argument(
        "--prefer-remote",
        action="store_true",
        help=(
            "For path-style local .tar.gz requirements, read FQCN/version from the "
            "tarball MANIFEST.json, then resolve download URL/checksum/size from "
            "galaxy server_list (warn if local and remote checksums differ)"
        ),
    )
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="Print JSON schema for optional ansible.in.yaml and exit",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(debug=args.debug)

    if args.print_schema:
        schema.print_schema()
        return 0

    from jsonschema import ValidationError

    project_dir = Path(args.project_dir).resolve()

    try:
        input_path = Path(args.input_file) if args.input_file else None
        if input_path is not None and not input_path.exists():
            logger.error("Input file not found: %s", input_path)
            return 1

        if input_path is not None and _is_ansible_in_file(input_path):
            logger.info(
                "Using ansible.in override %s "
                "(galaxy servers from contentOrigin; ansible.cfg not used)",
                input_path.resolve(),
            )
            requirements, servers = load_ansible_in(
                input_path, token_env_override=args.token_env
            )
        else:
            cfg = Path(args.ansible_cfg) if args.ansible_cfg else None
            if args.requirements:
                req: Path | None = Path(args.requirements)
            elif input_path is not None:
                req = input_path
            else:
                req = None
            requirements, servers = resolve_from_project(
                project_dir=project_dir,
                requirements_path=req,
                ansible_cfg_path=cfg,
                token_env_override=args.token_env,
            )

        collections, servers_used = galaxy.resolve_collections(
            requirements,
            servers=servers,
            prefer_remote=args.prefer_remote,
        )
    except ValidationError as exc:
        logger.error("Input validation failed: %s", exc.message)
        return 1
    except galaxy.GalaxyError as exc:
        logger.error("%s", exc)
        return 1

    outfile = Path(args.outfile)
    write_lockfile(collections, outfile, servers_used=servers_used)
    logger.info("Wrote %s (%d collections)", outfile, len(collections))

    if args.export_generic:
        generic_path = Path(args.export_generic)
        export_generic.write_generic_lockfile(
            collections, generic_path, servers=servers_used
        )
        logger.info("Wrote %s", generic_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
