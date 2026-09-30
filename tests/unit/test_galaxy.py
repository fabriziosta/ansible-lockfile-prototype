from __future__ import annotations

from pathlib import Path

import pytest
import responses
import yaml

from ansible_lockfile.export_generic import collections_to_generic, write_generic_lockfile
from ansible_lockfile.galaxy import (
    GalaxyError,
    load_requirements_file,
    resolve_collections,
)
from ansible_lockfile.models import CollectionItem


def test_load_requirements_file_ok(tmp_path: Path):
    path = tmp_path / "requirements.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "collections": [
                    {"name": "community.general", "version": "13.3.0"},
                    "ansible.posix",
                ]
            }
        ),
        encoding="utf-8",
    )
    reqs = load_requirements_file(path)
    assert reqs == [
        {"name": "community.general", "version": "13.3.0"},
        {"name": "ansible.posix"},
    ]


def test_load_requirements_rejects_roles(tmp_path: Path):
    path = tmp_path / "requirements.yml"
    path.write_text(
        yaml.safe_dump({"roles": [{"name": "geerlingguy.nginx"}]}),
        encoding="utf-8",
    )
    with pytest.raises(GalaxyError, match="roles are not supported"):
        load_requirements_file(path)


def test_load_requirements_rejects_git_type(tmp_path: Path):
    path = tmp_path / "requirements.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "collections": [
                    {
                        "name": "my.ns",
                        "type": "git",
                        "source": "https://example.com/my.ns.git",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GalaxyError, match="unsupported collection type"):
        load_requirements_file(path)


def test_collections_to_generic():
    items = [
        CollectionItem(
            name="community.general",
            version="13.3.0",
            url="https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/artifacts/community-general-13.3.0.tar.gz",
            checksum="sha256:abc",
            size=10,
        )
    ]
    data = collections_to_generic(items)
    assert data["metadata"]["version"] == "1.0"
    assert data["artifacts"] == [
        {
            "download_url": items[0].url,
            "checksum": "sha256:abc",
            "filename": "community-general-13.3.0.tar.gz",
        }
    ]


def test_write_generic_lockfile(tmp_path: Path):
    items = [
        CollectionItem(
            name="ansible.posix",
            version="1.5.4",
            url="https://example.com/ansible-posix-1.5.4.tar.gz",
            checksum="sha256:deadbeef",
            size=42,
        )
    ]
    out = tmp_path / "artifacts.lock.yaml"
    write_generic_lockfile(items, out)
    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert loaded["artifacts"][0]["filename"] == "ansible-posix-1.5.4.tar.gz"


def _version_list_payload(versions: list[str]) -> dict:
    return {
        "meta": {"count": len(versions)},
        "links": {"next": None},
        "data": [{"version": v} for v in versions],
    }


def _version_detail(
    namespace: str,
    name: str,
    version: str,
    *,
    sha256: str = "aa" * 32,
    size: int = 100,
    deps: dict | None = None,
) -> dict:
    filename = f"{namespace}-{name}-{version}.tar.gz"
    return {
        "version": version,
        "download_url": f"https://galaxy.ansible.com/download/{filename}",
        "artifact": {"filename": filename, "sha256": sha256, "size": size},
        "metadata": {"dependencies": deps or {}},
        "namespace": {"name": namespace},
        "name": name,
    }


@responses.activate
def test_resolve_with_transitive_dep():
    base = "https://galaxy.ansible.com"
    responses.add(
        responses.GET,
        f"{base}/api/v3/plugin/ansible/content/published/collections/index/community/general/versions/13.3.0/",
        json=_version_detail(
            "community",
            "general",
            "13.3.0",
            sha256="11" * 32,
            size=1000,
            deps={"community.library_inventory_filtering_v1": ">=1.0.0"},
        ),
    )
    responses.add(
        responses.GET,
        f"{base}/api/v3/plugin/ansible/content/published/collections/index/community/library_inventory_filtering_v1/versions/",
        json=_version_list_payload(["1.1.0", "1.0.2", "1.0.0"]),
    )
    responses.add(
        responses.GET,
        f"{base}/api/v3/plugin/ansible/content/published/collections/index/community/library_inventory_filtering_v1/versions/1.1.0/",
        json=_version_detail(
            "community",
            "library_inventory_filtering_v1",
            "1.1.0",
            sha256="22" * 32,
            size=200,
        ),
    )

    items, _used = resolve_collections(
        [{"name": "community.general", "version": "13.3.0"}],
        base_url=base,
    )
    names = [i.name for i in items]
    assert names == [
        "community.general",
        "community.library_inventory_filtering_v1",
    ]
    assert items[0].checksum == "sha256:" + "11" * 32
    assert items[1].version == "1.1.0"


@responses.activate
def test_resolve_conflict():
    base = "https://galaxy.ansible.com"
    responses.add(
        responses.GET,
        f"{base}/api/v3/plugin/ansible/content/published/collections/index/ns/a/versions/1.0.0/",
        json=_version_detail("ns", "a", "1.0.0", deps={"ns.b": "==1.0.0"}),
    )
    responses.add(
        responses.GET,
        f"{base}/api/v3/plugin/ansible/content/published/collections/index/ns/b/versions/2.0.0/",
        json=_version_detail("ns", "b", "2.0.0"),
    )
    responses.add(
        responses.GET,
        f"{base}/api/v3/plugin/ansible/content/published/collections/index/ns/b/versions/1.0.0/",
        json=_version_detail("ns", "b", "1.0.0"),
    )

    with pytest.raises(GalaxyError, match="Conflicting"):
        resolve_collections(
            [
                {"name": "ns.a", "version": "1.0.0"},
                {"name": "ns.b", "version": "2.0.0"},
            ],
            base_url=base,
        )
