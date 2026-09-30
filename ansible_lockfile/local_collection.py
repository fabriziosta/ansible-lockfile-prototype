from __future__ import annotations

import hashlib
import json
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import pathname2url

from .galaxy import GalaxyError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalCollectionMeta:
    path: Path
    namespace: str
    name: str
    version: str
    dependencies: dict[str, str]
    checksum: str
    size: int

    @property
    def fqcn(self) -> str:
        return f"{self.namespace}.{self.name}"

    def file_url(self) -> str:
        return urljoin("file:", pathname2url(str(self.path.resolve())))


def looks_like_tarball(value: str) -> bool:
    lower = value.lower()
    return lower.endswith(".tar.gz") or lower.endswith(".tgz")


def is_path_like_collection_name(value: str) -> bool:
    """True if name/source looks like a filesystem path rather than an FQCN."""
    if looks_like_tarball(value):
        return True
    if "/" in value or value.startswith("."):
        return True
    return False


def resolve_local_tarball(
    name: str,
    *,
    requirements_path: Path,
    project_dir: Path,
) -> Path:
    """Locate a local collection tarball using vendor-collections-friendly search paths."""
    path = Path(name)
    tried: list[str] = []
    candidates: list[Path] = []

    if path.is_absolute():
        candidates.append(path)
    else:
        req_dir = requirements_path.parent.resolve()
        project = project_dir.resolve()
        candidates.append(req_dir / path)
        # Parent of the requirements directory (repo root when req lives in ee-supported/).
        candidates.append(req_dir.parent / path)
        if req_dir != project:
            for parent in req_dir.parents:
                candidates.append(parent / path)
                if parent == project:
                    break
        candidates.append(project / path)

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate if candidate.is_absolute() else candidate
        key = resolved.resolve() if resolved.exists() else resolved.absolute()
        if key in seen:
            continue
        seen.add(key)
        tried.append(str(key))
        if candidate.is_file():
            logger.info("Using local collection tarball %s", candidate.resolve())
            return candidate.resolve()

    raise GalaxyError(
        f"Local collection tarball not found for {name!r}; tried: {', '.join(tried)}"
    )


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 64), b""):
            h.update(chunk)
            size += len(chunk)
    return f"sha256:{h.hexdigest()}", size


def is_git_lfs_pointer(path: Path) -> bool:
    """True if path looks like a Git LFS pointer file instead of binary content."""
    try:
        with path.open("rb") as fh:
            head = fh.read(256)
    except OSError:
        return False
    # LFS pointers are small UTF-8 text starting with the spec version line.
    if b"\0" in head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text.startswith("version https://git-lfs.github.com/spec/v1")


def _git_lfs_pointer_error(path: Path) -> GalaxyError:
    repo_hint = path.parent
    # Prefer the directory that likely owns .gitattributes / is the git root.
    for candidate in (path.parent, *path.parents):
        if (candidate / ".git").exists() or (candidate / ".gitattributes").exists():
            repo_hint = candidate
            break
    return GalaxyError(
        f"{path}: appears to be a Git LFS pointer, not the collection tarball. "
        f"Install Git LFS and pull the real artifacts, for example:\n"
        f"  sudo dnf install git-lfs   # or: sudo apt install git-lfs\n"
        f"  cd {repo_hint}\n"
        f"  git lfs install\n"
        f"  git lfs pull"
    )


def read_collection_tarball(path: Path) -> LocalCollectionMeta:
    """Read MANIFEST.json + checksum/size from a collection .tar.gz."""
    if not path.is_file():
        raise GalaxyError(f"Collection tarball does not exist: {path}")

    if is_git_lfs_pointer(path):
        logger.error(
            "%s is a Git LFS pointer; run git lfs pull in the owning repository",
            path,
        )
        raise _git_lfs_pointer_error(path)

    try:
        with tarfile.open(path, "r:gz") as tar:
            try:
                member = tar.getmember("MANIFEST.json")
            except KeyError as exc:
                raise GalaxyError(
                    f"{path}: archive is missing MANIFEST.json"
                ) from exc
            extracted = tar.extractfile(member)
            if extracted is None:
                raise GalaxyError(f"{path}: unable to read MANIFEST.json")
            try:
                manifest = json.load(extracted)
            except json.JSONDecodeError as exc:
                raise GalaxyError(f"{path}: invalid MANIFEST.json") from exc
    except GalaxyError:
        raise
    except tarfile.TarError as exc:
        # Catch LFS pointers we failed to detect, or truncated downloads.
        if is_git_lfs_pointer(path):
            raise _git_lfs_pointer_error(path) from exc
        raise GalaxyError(f"{path}: not a readable collection tar.gz: {exc}") from exc

    info = manifest.get("collection_info") or {}
    namespace = info.get("namespace")
    name = info.get("name")
    version = info.get("version")
    if not namespace or not name or version is None:
        raise GalaxyError(
            f"{path}: MANIFEST.json missing collection_info.namespace/name/version"
        )
    deps = info.get("dependencies") or {}
    if not isinstance(deps, dict):
        raise GalaxyError(f"{path}: MANIFEST.json dependencies must be a mapping")

    checksum, size = _hash_file(path)
    return LocalCollectionMeta(
        path=path.resolve(),
        namespace=str(namespace),
        name=str(name),
        version=str(version),
        dependencies={str(k): str(v) for k, v in deps.items()},
        checksum=checksum,
        size=size,
    )
