from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from .galaxy import GalaxyServer
from .models import CollectionItem


def collections_to_generic(
    collections: list[CollectionItem],
    *,
    servers: list[GalaxyServer] | None = None,
    server: GalaxyServer | None = None,
) -> dict:
    by_name: dict[str, GalaxyServer] = {}
    if servers:
        by_name = {s.name: s for s in servers}
    elif server is not None:
        by_name = {server.name: server}

    artifacts = []
    any_auth = False
    for item in collections:
        if item.server == "local" or item.url.startswith("file:"):
            continue
        filename = (
            Path(urlparse(item.url).path).name
            or f"{item.name.replace('.', '-')}-{item.version}.tar.gz"
        )
        entry: dict = {
            "download_url": item.url,
            "checksum": item.checksum,
            "filename": filename,
        }
        src = by_name.get(item.server) if item.server else None
        if src is None and len(by_name) == 1:
            src = next(iter(by_name.values()))
        bearer_env = src.generic_bearer_env() if src else None
        if bearer_env:
            any_auth = True
            entry["auth"] = {
                "bearer": {
                    "value": f"Bearer ${bearer_env}",
                }
            }
        artifacts.append(entry)

    return {
        "metadata": {"version": "2.0" if any_auth else "1.0"},
        "artifacts": artifacts,
    }


def write_generic_lockfile(
    collections: list[CollectionItem],
    path: Path,
    *,
    servers: list[GalaxyServer] | None = None,
    server: GalaxyServer | None = None,
) -> None:
    data = collections_to_generic(collections, servers=servers, server=server)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            data,
            fh,
            default_flow_style=False,
            sort_keys=False,
            explicit_start=True,
        )
