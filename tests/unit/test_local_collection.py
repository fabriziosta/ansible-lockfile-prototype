from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from ansible_lockfile.export_generic import collections_to_generic
from ansible_lockfile.galaxy import GalaxyError, load_requirements_file, resolve_collections
from ansible_lockfile.local_collection import (
    is_path_like_collection_name,
    read_collection_tarball,
    resolve_local_tarball,
)


def _make_collection_tarball(
    dest: Path,
    *,
    namespace: str = "amazon",
    name: str = "aws",
    version: str = "10.3.0",
    dependencies: dict | None = None,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "collection_info": {
            "namespace": namespace,
            "name": name,
            "version": version,
            "dependencies": dependencies or {},
        },
        "format": 1,
    }
    payload = json.dumps(manifest).encode("utf-8")
    with tarfile.open(dest, "w:gz") as tar:
        info = tarfile.TarInfo(name="MANIFEST.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return dest


def test_is_path_like():
    assert is_path_like_collection_name("collections/amazon-aws-10.3.0.tar.gz")
    assert is_path_like_collection_name("./foo.tgz")
    assert not is_path_like_collection_name("amazon.aws")


def test_resolve_local_tarball_vendor_layout(tmp_path: Path):
    repo = tmp_path / "vendor"
    tarball = _make_collection_tarball(
        repo / "collections" / "amazon-aws-10.3.0.tar.gz"
    )
    req = repo / "ee-supported" / "requirements-2.7.yml"
    req.parent.mkdir(parents=True)
    req.write_text("collections: []\n", encoding="utf-8")

    found = resolve_local_tarball(
        "collections/amazon-aws-10.3.0.tar.gz",
        requirements_path=req,
        project_dir=repo,
    )
    assert found == tarball.resolve()


def test_read_manifest_version(tmp_path: Path):
    tar = _make_collection_tarball(
        tmp_path / "ns-col-1.2.3.tar.gz",
        namespace="ns",
        name="col",
        version="1.2.3",
        dependencies={"other.dep": ">=1.0.0"},
    )
    meta = read_collection_tarball(tar)
    assert meta.fqcn == "ns.col"
    assert meta.version == "1.2.3"
    assert meta.dependencies == {"other.dep": ">=1.0.0"}
    assert meta.checksum.startswith("sha256:")
    assert meta.size > 0
    digest = hashlib.sha256(tar.read_bytes()).hexdigest()
    assert meta.checksum == f"sha256:{digest}"


def test_load_and_resolve_vendor_style_requirements(tmp_path: Path):
    repo = tmp_path / "vendor"
    _make_collection_tarball(
        repo / "collections" / "amazon-aws-10.3.0.tar.gz",
        namespace="amazon",
        name="aws",
        version="10.3.0",
    )
    req = repo / "ee-supported" / "requirements-2.7.yml"
    req.parent.mkdir(parents=True)
    req.write_text(
        yaml.safe_dump(
            {
                "collections": [
                    {"name": "collections/amazon-aws-10.3.0.tar.gz"},
                ]
            }
        ),
        encoding="utf-8",
    )

    requirements = load_requirements_file(req, project_dir=repo)
    assert len(requirements) == 1
    assert "local_path" in requirements[0]

    items, used = resolve_collections(requirements, servers=[])
    assert len(items) == 1
    assert items[0].name == "amazon.aws"
    assert items[0].version == "10.3.0"
    assert items[0].server == "local"
    assert items[0].url.startswith("file:")
    assert used[0].name == "local"

    generic = collections_to_generic(items, servers=used)
    assert generic["artifacts"] == []
    assert generic["metadata"]["version"] == "1.0"


def test_version_mismatch_fails(tmp_path: Path):
    repo = tmp_path
    _make_collection_tarball(
        repo / "collections" / "amazon-aws-10.3.0.tar.gz",
        version="10.3.0",
    )
    req = repo / "requirements.yml"
    req.write_text(
        yaml.safe_dump(
            {
                "collections": [
                    {
                        "name": "collections/amazon-aws-10.3.0.tar.gz",
                        "version": "9.0.0",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    requirements = load_requirements_file(req, project_dir=repo)
    with pytest.raises(GalaxyError, match="does not match MANIFEST"):
        resolve_collections(requirements, servers=[])


def test_missing_tarball_lists_tried_paths(tmp_path: Path):
    req = tmp_path / "ee-supported" / "requirements.yml"
    req.parent.mkdir(parents=True)
    req.write_text(
        yaml.safe_dump(
            {"collections": [{"name": "collections/missing-1.0.0.tar.gz"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(GalaxyError, match="tried:"):
        load_requirements_file(req, project_dir=tmp_path)


def test_git_lfs_pointer_gives_actionable_error(tmp_path: Path):
    pointer = tmp_path / "amazon-aws-10.3.0.tar.gz"
    pointer.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:6496fc315513cba8c48125cfda4e464d74e3fb43616df784b1ceb14b1290b951\n"
        "size 1314805\n",
        encoding="utf-8",
    )
    with pytest.raises(GalaxyError, match="Git LFS pointer"):
        read_collection_tarball(pointer)
    with pytest.raises(GalaxyError, match="git lfs pull"):
        read_collection_tarball(pointer)
