from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from .models import CollectionItem

logger = logging.getLogger(__name__)

DEFAULT_GALAXY_URL = "https://galaxy.ansible.com"
DEFAULT_CLIENT_ID = "cloud-services"
DEFAULT_ACCESS_TOKEN_ENV = "AUTOMATION_HUB_ACCESS_TOKEN"
CACHE_ENV = "ANSIBLE_LOCKFILE_PROTOTYPE_CACHE"
DEFAULT_CACHE = Path.home() / ".cache" / "ansible-lockfile-prototype"

UNSUPPORTED_TYPES = {"git", "dir", "url", "subdirs"}
LOCAL_SERVER_NAME = "local"


class GalaxyError(Exception):
    """Raised when Galaxy resolution or download fails."""


class GalaxyNotFound(GalaxyError):
    """Collection or version was not found on a server (safe to try the next)."""


@dataclass(frozen=True)
class GalaxyServer:
    """Galaxy / Automation Hub server configuration."""

    name: str = "galaxy"
    url: str = DEFAULT_GALAXY_URL
    auth_url: str | None = None
    token: str | None = None
    token_env: str | None = None
    client_id: str = DEFAULT_CLIENT_ID
    username: str | None = None
    password: str | None = None

    def is_supported(self) -> bool:
        """Username/password-only private hubs are not supported yet."""
        if (self.username or self.password) and not (self.token or self.token_env):
            return False
        return True

    def needs_auth(self) -> bool:
        return bool(self.token or self.token_env)

    def auth_metadata(self) -> dict | None:
        """Env-var / SSO refs for ansible.lock.yaml (never the secret value)."""
        if not self.needs_auth() and not self.auth_url:
            return None
        meta: dict = {"name": self.name, "url": self.url}
        if self.token_env:
            meta["token_env"] = self.token_env
        elif self.token:
            meta["token_from"] = "ansible.cfg"
        if self.auth_url:
            meta["auth_url"] = self.auth_url
            meta["client_id"] = self.client_id
        return meta

    def generic_bearer_env(self) -> str | None:
        """Env var Hermeto should read for Bearer auth on generic export."""
        if not self.needs_auth() and not self.auth_url:
            return None
        if self.auth_url:
            return DEFAULT_ACCESS_TOKEN_ENV
        if self.token_env:
            return self.token_env
        # Literal token in ansible.cfg — Hermeto still needs an env at prefetch.
        return DEFAULT_ACCESS_TOKEN_ENV if self.token else None


def _cache_dir() -> Path:
    raw = os.environ.get(CACHE_ENV)
    path = Path(raw) if raw else DEFAULT_CACHE
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_base(url: str) -> str:
    return url.rstrip("/") + "/"


def _split_name(name: str) -> tuple[str, str]:
    parts = name.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise GalaxyError(f"Invalid collection name {name!r}; expected namespace.name")
    return parts[0], parts[1]


def _collections_index_prefix(base_url: str) -> str:
    """Return the collections index path prefix relative to base_url.

    - ``.../content/<repo>/`` (Automation Hub ansible.cfg style) → ``v3/collections/``
    - URL already under ``/api/`` → plugin index without leading ``api/``
    - Host root (galaxy.ansible.com) → ``api/v3/plugin/...``
    """
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if "/content/" in path:
        return "v3/collections/"
    if "/api/" in path or path.endswith("/api"):
        return "v3/plugin/ansible/content/published/collections/index/"
    return "api/v3/plugin/ansible/content/published/collections/index/"


def _versions_api_path(base_url: str, namespace: str, name: str) -> str:
    return f"{_collections_index_prefix(base_url)}{namespace}/{name}/versions/"


def _version_api_path(base_url: str, namespace: str, name: str, version: str) -> str:
    return _versions_api_path(base_url, namespace, name) + f"{version}/"


def _parse_constraint(constraint: str | None) -> SpecifierSet | None:
    if constraint is None or constraint.strip() in ("", "*", "latest"):
        return None
    text = constraint.strip()
    try:
        Version(text)
        return SpecifierSet(f"=={text}")
    except InvalidVersion:
        pass
    try:
        return SpecifierSet(text)
    except Exception as exc:
        raise GalaxyError(f"Invalid version constraint {constraint!r}: {exc}") from exc


def _merge_constraints(
    existing: SpecifierSet | None,
    new: SpecifierSet | None,
    *,
    name: str,
) -> SpecifierSet | None:
    if existing is None:
        return new
    if new is None:
        return existing
    existing_eq = {s.version for s in existing if s.operator == "=="}
    new_eq = {s.version for s in new if s.operator == "=="}
    if existing_eq and new_eq and existing_eq.isdisjoint(new_eq):
        raise GalaxyError(
            f"Conflicting version pins for {name}: {existing} vs {new}"
        )
    return existing & new


def _version_matches(version: str, spec: SpecifierSet | None) -> bool:
    try:
        ver = Version(version)
    except InvalidVersion:
        return False
    if spec is None:
        return True
    return ver in spec


def _resolve_secret(server: GalaxyServer) -> str:
    if server.token_env:
        value = os.environ.get(server.token_env)
        if not value:
            raise GalaxyError(
                f"Environment variable {server.token_env!r} is not set or empty "
                f"(required for server {server.name!r})"
            )
        return value
    if server.token:
        return server.token
    raise GalaxyError(f"Server {server.name!r} requires a token or token_env")


class GalaxyClient:
    def __init__(
        self,
        server: GalaxyServer | None = None,
        *,
        base_url: str | None = None,
        session: requests.Session | None = None,
    ):
        if server is None:
            server = GalaxyServer(url=base_url or DEFAULT_GALAXY_URL)
        elif base_url is not None and base_url != server.url:
            server = GalaxyServer(
                name=server.name,
                url=base_url,
                auth_url=server.auth_url,
                token=server.token,
                token_env=server.token_env,
                client_id=server.client_id,
                username=server.username,
                password=server.password,
            )
        self.server = server
        self.base_url = _normalize_base(server.url)
        self.session = session or requests.Session()
        self._auth_ready = False

    def _ensure_auth(self) -> None:
        if self._auth_ready:
            return
        if not self.server.needs_auth():
            self._auth_ready = True
            return
        token = _resolve_secret(self.server)
        if self.server.auth_url:
            access = self._exchange_refresh_token(token)
            self.session.headers["Authorization"] = f"Bearer {access}"
            logger.debug(
                "Server %s: authenticated via SSO (Bearer)", self.server.name
            )
        else:
            self.session.headers["Authorization"] = f"Token {token}"
            logger.debug(
                "Server %s: authenticated via API token", self.server.name
            )
        self._auth_ready = True

    def _exchange_refresh_token(self, refresh_token: str) -> str:
        assert self.server.auth_url is not None
        data = {
            "grant_type": "refresh_token",
            "client_id": self.server.client_id,
            "refresh_token": refresh_token,
        }
        logger.debug("POST %s (SSO token exchange)", self.server.auth_url)
        try:
            resp = requests.post(self.server.auth_url, data=data, timeout=60)
        except requests.RequestException as exc:
            raise GalaxyError(
                f"SSO token exchange failed for {self.server.auth_url}: {exc}"
            ) from exc
        if not resp.ok:
            raise GalaxyError(
                f"SSO token exchange HTTP {resp.status_code} for {self.server.auth_url}: "
                f"{resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GalaxyError("SSO token exchange returned non-JSON body") from exc
        access = payload.get("access_token")
        if not access:
            raise GalaxyError("SSO token exchange response missing access_token")
        return access

    def _get_json(self, path_or_url: str) -> dict:
        self._ensure_auth()
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = urljoin(self.base_url, path_or_url.lstrip("/"))
        logger.debug("GET %s", url)
        try:
            resp = self.session.get(url, timeout=60)
        except requests.RequestException as exc:
            raise GalaxyError(f"Galaxy request failed for {url}: {exc}") from exc
        if resp.status_code == 404:
            raise GalaxyNotFound(f"Galaxy resource not found: {url}")
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise GalaxyError(f"Galaxy HTTP error for {url}: {exc}") from exc
        return resp.json()

    def list_versions(self, namespace: str, name: str) -> list[str]:
        versions: list[str] = []
        next_url: str | None = (
            _versions_api_path(self.base_url, namespace, name) + "?limit=100"
        )
        while next_url:
            payload = self._get_json(next_url)
            for item in payload.get("data", []):
                ver = item.get("version")
                if ver:
                    versions.append(ver)
            links = payload.get("links") or {}
            nxt = links.get("next")
            if not nxt:
                break
            if nxt.startswith("http://") or nxt.startswith("https://"):
                next_url = nxt
            else:
                parsed = urlparse(self.base_url)
                next_url = f"{parsed.scheme}://{parsed.netloc}{nxt}"
        return versions

    def get_version(self, namespace: str, name: str, version: str) -> dict:
        return self._get_json(
            _version_api_path(self.base_url, namespace, name, version)
        )

    def resolve_version(self, fqcn: str, constraint: str | None) -> str:
        namespace, name = _split_name(fqcn)
        spec = _parse_constraint(constraint)
        if spec is not None and list(spec) and all(s.operator == "==" for s in spec):
            exact = next(iter(spec)).version
            self.get_version(namespace, name, exact)
            return exact

        try:
            versions = self.list_versions(namespace, name)
        except GalaxyNotFound:
            raise
        if not versions:
            raise GalaxyNotFound(f"No versions found for {fqcn} on {self.server.name}")

        matching: list[Version] = []
        for ver in versions:
            if _version_matches(ver, spec):
                try:
                    matching.append(Version(ver))
                except InvalidVersion:
                    continue
        if not matching:
            raise GalaxyNotFound(
                f"No version of {fqcn} matches {constraint!r} on {self.server.name}"
            )
        return str(max(matching))


def _collection_item_from_version(
    fqcn: str,
    payload: dict,
    *,
    session: requests.Session | None = None,
    server_name: str = "",
) -> CollectionItem:
    artifact = payload.get("artifact") or {}
    download_url = payload.get("download_url")
    if not download_url:
        raise GalaxyError(f"Missing download_url for {fqcn}@{payload.get('version')}")
    sha256 = artifact.get("sha256")
    size = artifact.get("size")
    if not sha256:
        sha256 = _download_and_hash(download_url, session=session)
    if size is None:
        size = _download_size(download_url, session=session)
    version = payload.get("version")
    if not version:
        raise GalaxyError(f"Missing version in Galaxy payload for {fqcn}")
    return CollectionItem(
        name=fqcn,
        version=str(version),
        url=download_url,
        checksum=f"sha256:{sha256}",
        size=int(size),
        server=server_name,
    )


def _download_and_hash(url: str, session: requests.Session | None = None) -> str:
    session = session or requests.Session()
    cache = _cache_dir()
    name = Path(urlparse(url).path).name or "artifact.tar.gz"
    dest = cache / name
    if not dest.exists():
        logger.debug("Downloading %s -> %s", url, dest)
        with session.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        fh.write(chunk)
    h = hashlib.sha256()
    with dest.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 64), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_size(url: str, session: requests.Session | None = None) -> int:
    session = session or requests.Session()
    cache = _cache_dir()
    name = Path(urlparse(url).path).name or "artifact.tar.gz"
    dest = cache / name
    if dest.exists():
        return dest.stat().st_size
    head = session.head(url, allow_redirects=True, timeout=60)
    if head.ok and head.headers.get("Content-Length"):
        return int(head.headers["Content-Length"])
    _download_and_hash(url, session=session)
    return dest.stat().st_size


def _dependencies_from_payload(payload: dict) -> dict[str, str]:
    metadata = payload.get("metadata") or {}
    deps = metadata.get("dependencies") or {}
    if not isinstance(deps, dict):
        raise GalaxyError(f"Unexpected dependencies format: {deps!r}")
    return {str(k): str(v) for k, v in deps.items()}


def _usable_servers(servers: list[GalaxyServer]) -> list[GalaxyServer]:
    usable: list[GalaxyServer] = []
    for server in servers:
        if not server.is_supported():
            logger.warning(
                "Skipping galaxy server %r (username/password auth is not supported)",
                server.name,
            )
            continue
        usable.append(server)
    if not usable:
        raise GalaxyError(
            "No usable galaxy servers (all entries require unsupported username/password auth)"
        )
    return usable


def _resolve_on_servers(
    fqcn: str,
    constraint_text: str | None,
    clients: list[GalaxyClient],
) -> tuple[CollectionItem, GalaxyClient, dict]:
    errors: list[str] = []
    for client in clients:
        try:
            namespace, name = _split_name(fqcn)
            version = client.resolve_version(fqcn, constraint_text)
            payload = client.get_version(namespace, name, version)
            item = _collection_item_from_version(
                fqcn,
                payload,
                session=client.session,
                server_name=client.server.name,
            )
            logger.info(
                "Resolved %s==%s from server %r",
                fqcn,
                version,
                client.server.name,
            )
            return item, client, payload
        except GalaxyNotFound as exc:
            logger.debug(
                "%s not found on %r: %s", fqcn, client.server.name, exc
            )
            errors.append(f"{client.server.name}: {exc}")
            continue
    raise GalaxyError(
        f"Could not resolve {fqcn} on any galaxy server"
        + (f" ({'; '.join(errors)})" if errors else "")
    )


def resolve_collections(
    requirements: list[dict],
    *,
    servers: list[GalaxyServer] | None = None,
    server: GalaxyServer | None = None,
    base_url: str | None = None,
    session: requests.Session | None = None,
) -> tuple[list[CollectionItem], list[GalaxyServer]]:
    """Resolve collections trying servers in priority order (ansible server_list).

    Local tarball requirements (``local_path``) are resolved from MANIFEST.json
    and never hit Galaxy/AH.

    Returns ``(items, servers_used)`` where ``servers_used`` are unique servers
    that supplied at least one collection (for lockfile auth metadata).
    """
    from . import local_collection

    if servers is None:
        if server is not None:
            servers = [server]
        else:
            servers = [GalaxyServer(url=base_url or DEFAULT_GALAXY_URL)]

    has_remote = any(req.get("local_path") is None for req in requirements)
    clients: list[GalaxyClient] = []
    if has_remote:
        usable = _usable_servers(servers)
        if session is not None and len(usable) == 1:
            clients = [GalaxyClient(server=usable[0], session=session)]
        else:
            clients = [GalaxyClient(server=s) for s in usable]

    local_meta_by_fqcn: dict[str, "local_collection.LocalCollectionMeta"] = {}
    constraints: dict[str, SpecifierSet | None] = {}

    for req in requirements:
        local_path = req.get("local_path")
        if local_path is not None:
            meta = local_collection.read_collection_tarball(Path(local_path))
            if "version" in req and req["version"] is not None:
                pinned = str(req["version"])
                if pinned not in ("*", "latest", "") and pinned != meta.version:
                    raise GalaxyError(
                        f"Local collection {meta.path}: requirements version "
                        f"{pinned!r} does not match MANIFEST.json version {meta.version!r}"
                    )
            fqcn = meta.fqcn
            if fqcn in local_meta_by_fqcn:
                existing = local_meta_by_fqcn[fqcn]
                if existing.path != meta.path:
                    raise GalaxyError(
                        f"Conflicting local tarballs for {fqcn}: "
                        f"{existing.path} vs {meta.path}"
                    )
            local_meta_by_fqcn[fqcn] = meta
            constraints[fqcn] = _merge_constraints(
                constraints.get(fqcn),
                _parse_constraint(meta.version),
                name=fqcn,
            )
            continue

        name = req["name"]
        spec = _parse_constraint(req.get("version"))
        constraints[name] = _merge_constraints(
            constraints.get(name), spec, name=name
        )

    resolved: dict[str, CollectionItem] = {}
    used_servers: dict[str, GalaxyServer] = {}
    queue: list[str] = list(constraints.keys())

    def _add_deps(deps: dict[str, str], from_fqcn: str) -> None:
        for dep_name, dep_constraint in deps.items():
            dep_spec = _parse_constraint(dep_constraint)
            if dep_name in resolved:
                if not _version_matches(resolved[dep_name].version, dep_spec):
                    raise GalaxyError(
                        f"Conflicting dependency for {dep_name}: "
                        f"already locked to {resolved[dep_name].version}, "
                        f"but {from_fqcn} requires {dep_constraint!r}"
                    )
                continue
            if dep_name in constraints or dep_name in resolved:
                constraints[dep_name] = _merge_constraints(
                    constraints.get(dep_name), dep_spec, name=dep_name
                )
            else:
                constraints[dep_name] = dep_spec
            if dep_name not in resolved and dep_name not in queue:
                queue.append(dep_name)

    while queue:
        fqcn = queue.pop(0)
        if fqcn in resolved:
            continue

        if fqcn in local_meta_by_fqcn:
            meta = local_meta_by_fqcn[fqcn]
            if not _version_matches(meta.version, constraints.get(fqcn)):
                raise GalaxyError(
                    f"Local {fqcn}=={meta.version} does not satisfy {constraints.get(fqcn)}"
                )
            item = CollectionItem(
                name=meta.fqcn,
                version=meta.version,
                url=meta.file_url(),
                checksum=meta.checksum,
                size=meta.size,
                server=LOCAL_SERVER_NAME,
            )
            resolved[fqcn] = item
            used_servers[LOCAL_SERVER_NAME] = GalaxyServer(
                name=LOCAL_SERVER_NAME, url="file://"
            )
            logger.info("Resolved %s==%s from local tarball %s", fqcn, meta.version, meta.path)
            _add_deps(meta.dependencies, fqcn)
            continue

        constraint_text = None
        spec = constraints.get(fqcn)
        if spec is not None:
            constraint_text = str(spec)
        if not clients:
            raise GalaxyError(
                f"Cannot resolve remote collection {fqcn}: no usable galaxy servers configured"
            )
        item, client, payload = _resolve_on_servers(fqcn, constraint_text, clients)
        if not _version_matches(item.version, constraints.get(fqcn)):
            raise GalaxyError(
                f"Resolved {fqcn}=={item.version} does not satisfy {constraints.get(fqcn)}"
            )
        resolved[fqcn] = item
        used_servers[client.server.name] = client.server
        _add_deps(_dependencies_from_payload(payload), fqcn)

    items = sorted(resolved.values(), key=lambda c: c.name)
    return items, list(used_servers.values())


def load_requirements_file(
    path: Path,
    *,
    project_dir: Path | None = None,
) -> list[dict]:
    """Load collections from a Galaxy requirements.yml; infer local tarballs."""
    import yaml

    from . import local_collection

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    if isinstance(data, list):
        raise GalaxyError(
            f"{path}: bare list requirements are treated as roles; "
            "use a mapping with a 'collections:' key"
        )

    if not isinstance(data, dict):
        raise GalaxyError(f"{path}: expected a YAML mapping")

    if data.get("roles") and not data.get("collections"):
        raise GalaxyError(f"{path}: roles are not supported in this prototype")
    if data.get("roles"):
        logger.warning("%s: ignoring roles: section (collections only)", path)

    collections = data.get("collections")
    if collections is None:
        raise GalaxyError(f"{path}: missing 'collections' key")
    if not isinstance(collections, list):
        raise GalaxyError(f"{path}: 'collections' must be a list")

    root = (project_dir or path.parent).resolve()
    result: list[dict] = []
    for entry in collections:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            raise GalaxyError(f"{path}: invalid collection entry {entry!r}")

        src_type = entry.get("type", "galaxy")
        if src_type in UNSUPPORTED_TYPES:
            raise GalaxyError(
                f"{path}: unsupported collection type {src_type!r} "
                f"for {entry.get('name', entry.get('source'))}"
            )

        raw_name = entry.get("name")
        raw_source = entry.get("source")
        path_candidate = None
        if isinstance(raw_name, str) and local_collection.is_path_like_collection_name(
            raw_name
        ):
            path_candidate = raw_name
        elif isinstance(raw_source, str) and local_collection.is_path_like_collection_name(
            raw_source
        ):
            path_candidate = raw_source
        elif src_type == "file":
            path_candidate = raw_name or raw_source

        if path_candidate:
            local_path = local_collection.resolve_local_tarball(
                path_candidate,
                requirements_path=path,
                project_dir=root,
            )
            item: dict = {"local_path": str(local_path), "name": path_candidate}
            if "version" in entry and entry["version"] is not None:
                item["version"] = str(entry["version"])
            result.append(item)
            continue

        if "name" not in entry:
            raise GalaxyError(f"{path}: collection entry missing 'name': {entry!r}")
        if src_type not in ("galaxy", "file", None):
            raise GalaxyError(
                f"{path}: unsupported collection type {src_type!r} for {entry['name']}"
            )
        item = {"name": entry["name"]}
        if "version" in entry and entry["version"] is not None:
            item["version"] = str(entry["version"])
        if entry.get("source") and str(entry["source"]).endswith(".git"):
            raise GalaxyError(
                f"{path}: git sources are not supported for {entry['name']}"
            )
        result.append(item)
    return result


def server_from_mapping(
    entry: dict | None,
    *,
    token_env_override: str | None = None,
) -> GalaxyServer:
    """Build GalaxyServer from contentOrigin.galaxy[0] mapping."""
    if not entry:
        server = GalaxyServer()
    else:
        server = GalaxyServer(
            name=entry.get("name") or "galaxy",
            url=entry["url"],
            auth_url=entry.get("auth_url"),
            token_env=entry.get("token_env"),
            client_id=entry.get("client_id") or DEFAULT_CLIENT_ID,
        )
    if token_env_override:
        server = GalaxyServer(
            name=server.name,
            url=server.url,
            auth_url=server.auth_url,
            token=None,
            token_env=token_env_override,
            client_id=server.client_id,
        )
    if server.auth_url and not (server.token or server.token_env):
        raise GalaxyError("auth_url requires token= / token_env (or --token-env)")
    return server
