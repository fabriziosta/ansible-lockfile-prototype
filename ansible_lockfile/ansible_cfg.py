from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path

from .galaxy import DEFAULT_CLIENT_ID, DEFAULT_GALAXY_URL, GalaxyError, GalaxyServer

logger = logging.getLogger(__name__)


def find_ansible_cfg(start: Path | None = None) -> Path | None:
    """Locate ansible.cfg using ansible-compatible search order.

    Order: ``$ANSIBLE_CONFIG``, ``./ansible.cfg`` (from start), then walk
    parents for ``ansible.cfg``, then ``~/.ansible.cfg``, then
    ``/etc/ansible/ansible.cfg``.
    """
    env = os.environ.get("ANSIBLE_CONFIG")
    if env:
        path = Path(env).expanduser()
        if path.is_file():
            return path.resolve()
        raise GalaxyError(f"ANSIBLE_CONFIG is set but not a file: {env}")

    start = (start or Path.cwd()).resolve()
    if start.is_file():
        start = start.parent

    candidate = start / "ansible.cfg"
    if candidate.is_file():
        return candidate

    for parent in start.parents:
        candidate = parent / "ansible.cfg"
        if candidate.is_file():
            return candidate

    home_cfg = Path.home() / ".ansible.cfg"
    if home_cfg.is_file():
        return home_cfg

    etc_cfg = Path("/etc/ansible/ansible.cfg")
    if etc_cfg.is_file():
        return etc_cfg

    return None


def parse_ansible_cfg(
    path: Path,
    *,
    token_env_override: str | None = None,
) -> list[GalaxyServer]:
    """Parse galaxy server_list from ansible.cfg into GalaxyServer entries.

    Servers that only have username/password (no token) are included but marked
    unsupported via :meth:`GalaxyServer.is_supported` and skipped at resolve time.
    """
    parser = configparser.ConfigParser()
    read = parser.read(path)
    if not read:
        raise GalaxyError(f"Unable to read ansible.cfg: {path}")

    if parser.has_section("galaxy") and parser.has_option("galaxy", "server_list"):
        names = [
            n.strip()
            for n in parser.get("galaxy", "server_list").split(",")
            if n.strip()
        ]
    else:
        names = []

    servers: list[GalaxyServer] = []
    for name in names:
        section = f"galaxy_server.{name}"
        if not parser.has_section(section):
            raise GalaxyError(
                f"{path}: server_list entry {name!r} has no [{section}] section"
            )
        url = parser.get(section, "url", fallback="").strip()
        if not url:
            raise GalaxyError(f"{path}: [{section}] missing required url")
        auth_url = parser.get(section, "auth_url", fallback="").strip() or None
        token = parser.get(section, "token", fallback="").strip() or None
        cfg_token_env = parser.get(section, "token_env", fallback="").strip() or None
        client_id = (
            parser.get(section, "client_id", fallback="").strip() or DEFAULT_CLIENT_ID
        )
        username = parser.get(section, "username", fallback="").strip() or None
        password = parser.get(section, "password", fallback="").strip() or None
        # CLI --token-env wins over ansible.cfg token_env= / token=.
        token_env = token_env_override or cfg_token_env
        servers.append(
            GalaxyServer(
                name=name,
                url=url,
                auth_url=auth_url,
                token=None if token_env else token,
                token_env=token_env,
                client_id=client_id,
                username=username,
                password=password,
            )
        )

    if not servers:
        # No galaxy server_list — default to public Galaxy (ansible-galaxy default).
        logger.info(
            "%s: no [galaxy] server_list; defaulting to %s",
            path,
            DEFAULT_GALAXY_URL,
        )
        servers.append(
            GalaxyServer(
                name="galaxy",
                url=DEFAULT_GALAXY_URL,
                token_env=token_env_override,
            )
        )

    for server in servers:
        if server.auth_url and not (server.token or server.token_env):
            raise GalaxyError(
                f"{path}: galaxy_server.{server.name} has auth_url but no token "
                "(set token= or token_env= in ansible.cfg, or pass --token-env)"
            )

    return servers
