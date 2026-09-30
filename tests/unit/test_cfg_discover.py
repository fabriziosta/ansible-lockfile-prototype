from __future__ import annotations

from pathlib import Path

import pytest
import responses
import yaml

from ansible_lockfile.ansible_cfg import find_ansible_cfg, parse_ansible_cfg
from ansible_lockfile.discover import find_requirements_file
from ansible_lockfile.export_generic import collections_to_generic
from ansible_lockfile.galaxy import (
    DEFAULT_ACCESS_TOKEN_ENV,
    GalaxyError,
    GalaxyServer,
    resolve_collections,
)
from ansible_lockfile.models import CollectionItem


def test_parse_ansible_cfg_server_list(tmp_path: Path):
    cfg = tmp_path / "ansible.cfg"
    cfg.write_text(
        """
[galaxy]
server_list = automation_hub, galaxy

[galaxy_server.automation_hub]
url=https://console.redhat.com/api/automation-hub/content/published/
auth_url=https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token
token=my_ah_token

[galaxy_server.galaxy]
url=https://galaxy.ansible.com
""",
        encoding="utf-8",
    )
    servers = parse_ansible_cfg(cfg)
    assert [s.name for s in servers] == ["automation_hub", "galaxy"]
    assert servers[0].token == "my_ah_token"
    assert servers[0].auth_url.endswith("/token")
    assert servers[1].url == "https://galaxy.ansible.com"


def test_parse_skips_need_for_unsupported_userpass_but_keeps_entry(tmp_path: Path):
    cfg = tmp_path / "ansible.cfg"
    cfg.write_text(
        """
[galaxy]
server_list = automation_hub, my_org_hub

[galaxy_server.automation_hub]
url=https://console.redhat.com/api/automation-hub/content/published/
auth_url=https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token
token=my_ah_token

[galaxy_server.my_org_hub]
url=https://automation.my_org/api/galaxy/content/rh-certified/
username=my_user
password=my_pass
""",
        encoding="utf-8",
    )
    servers = parse_ansible_cfg(cfg)
    assert servers[1].is_supported() is False
    assert servers[0].is_supported() is True


def test_parse_auth_url_without_token_fails(tmp_path: Path):
    cfg = tmp_path / "ansible.cfg"
    cfg.write_text(
        """
[galaxy]
server_list = automation_hub

[galaxy_server.automation_hub]
url=https://console.redhat.com/api/automation-hub/content/published/
auth_url=https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token
""",
        encoding="utf-8",
    )
    with pytest.raises(GalaxyError, match="auth_url but no token"):
        parse_ansible_cfg(cfg)


def test_token_env_override_clears_cfg_token(tmp_path: Path):
    cfg = tmp_path / "ansible.cfg"
    cfg.write_text(
        """
[galaxy]
server_list = automation_hub

[galaxy_server.automation_hub]
url=https://console.redhat.com/api/automation-hub/content/published/
auth_url=https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token
token=from_cfg
""",
        encoding="utf-8",
    )
    servers = parse_ansible_cfg(cfg, token_env_override="AUTOMATION_HUB_TOKEN")
    assert servers[0].token is None
    assert servers[0].token_env == "AUTOMATION_HUB_TOKEN"


def test_find_ansible_cfg_and_requirements(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANSIBLE_CONFIG", raising=False)
    (tmp_path / "ansible.cfg").write_text("[galaxy]\n", encoding="utf-8")
    (tmp_path / "requirements.yml").write_text(
        "collections:\n  - name: community.general\n",
        encoding="utf-8",
    )
    assert find_ansible_cfg(tmp_path) == (tmp_path / "ansible.cfg").resolve()
    assert find_requirements_file(tmp_path) == tmp_path / "requirements.yml"


AH_BASE = "https://console.redhat.com/api/automation-hub/content/published/"
SSO_URL = (
    "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
)
GALAXY = "https://galaxy.ansible.com"


@responses.activate
def test_server_list_failover_to_second():
    hub = GalaxyServer(
        name="automation_hub",
        url=AH_BASE,
        auth_url=SSO_URL,
        token="refresh",
    )
    galaxy = GalaxyServer(name="galaxy", url=GALAXY)

    # Hub: collection missing
    responses.add(
        responses.POST,
        SSO_URL,
        json={"access_token": "access"},
    )
    responses.add(
        responses.GET,
        f"{AH_BASE.rstrip('/')}/v3/collections/community/general/versions/13.3.0/",
        status=404,
    )
    # Galaxy: found
    responses.add(
        responses.GET,
        f"{GALAXY}/api/v3/plugin/ansible/content/published/collections/index/community/general/versions/13.3.0/",
        json={
            "version": "13.3.0",
            "download_url": "https://galaxy.ansible.com/dl/community-general-13.3.0.tar.gz",
            "artifact": {
                "filename": "community-general-13.3.0.tar.gz",
                "sha256": "11" * 32,
                "size": 10,
            },
            "metadata": {"dependencies": {}},
            "namespace": {"name": "community"},
            "name": "general",
        },
    )

    items, used = resolve_collections(
        [{"name": "community.general", "version": "13.3.0"}],
        servers=[hub, galaxy],
    )
    assert items[0].server == "galaxy"
    assert [s.name for s in used] == ["galaxy"]


@responses.activate
def test_stops_on_first_server_hit():
    hub = GalaxyServer(
        name="automation_hub",
        url=AH_BASE,
        auth_url=SSO_URL,
        token="refresh",
    )
    galaxy = GalaxyServer(name="galaxy", url=GALAXY)

    responses.add(
        responses.POST,
        SSO_URL,
        json={"access_token": "access"},
    )
    responses.add(
        responses.GET,
        f"{AH_BASE.rstrip('/')}/v3/collections/kubernetes/core/versions/6.1.0/",
        json={
            "version": "6.1.0",
            "download_url": "https://example.com/kubernetes-core-6.1.0.tar.gz",
            "artifact": {
                "filename": "kubernetes-core-6.1.0.tar.gz",
                "sha256": "22" * 32,
                "size": 20,
            },
            "metadata": {"dependencies": {}},
            "namespace": {"name": "kubernetes"},
            "name": "core",
        },
    )

    items, used = resolve_collections(
        [{"name": "kubernetes.core", "version": "6.1.0"}],
        servers=[hub, galaxy],
    )
    assert items[0].server == "automation_hub"
    assert used[0].name == "automation_hub"
    assert all("galaxy.ansible.com" not in (c.request.url or "") for c in responses.calls)


def test_generic_export_uses_server_auth():
    items = [
        CollectionItem(
            name="kubernetes.core",
            version="6.1.0",
            url="https://example.com/x.tar.gz",
            checksum="sha256:ab",
            size=1,
            server="automation_hub",
        )
    ]
    servers = [
        GalaxyServer(
            name="automation_hub",
            url=AH_BASE,
            auth_url=SSO_URL,
            token="x",
        )
    ]
    data = collections_to_generic(items, servers=servers)
    assert data["metadata"]["version"] == "2.0"
    assert data["artifacts"][0]["auth"]["bearer"]["value"] == (
        f"Bearer ${DEFAULT_ACCESS_TOKEN_ENV}"
    )
