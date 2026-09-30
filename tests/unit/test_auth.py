from __future__ import annotations

from pathlib import Path

import pytest
import responses
import yaml

from ansible_lockfile import main as cli_main
from ansible_lockfile.export_generic import collections_to_generic
from ansible_lockfile.galaxy import (
    DEFAULT_ACCESS_TOKEN_ENV,
    GalaxyError,
    GalaxyServer,
    resolve_collections,
    server_from_mapping,
)
from ansible_lockfile.models import CollectionItem


AH_BASE = "https://console.redhat.com/api/automation-hub/"
SSO_URL = (
    "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
)


def test_server_from_mapping_requires_token_env_with_auth_url():
    with pytest.raises(GalaxyError, match="auth_url requires token"):
        server_from_mapping(
            {
                "url": AH_BASE,
                "auth_url": SSO_URL,
            }
        )


def test_server_from_mapping_token_env_override():
    server = server_from_mapping(
        {
            "url": AH_BASE,
            "auth_url": SSO_URL,
            "token_env": "OLD_TOKEN",
        },
        token_env_override="NEW_TOKEN",
    )
    assert server.token_env == "NEW_TOKEN"
    assert server.auth_url == SSO_URL


def test_auth_metadata_and_generic_bearer_env():
    public = GalaxyServer()
    assert public.auth_metadata() is None
    assert public.generic_bearer_env() is None

    token_only = GalaxyServer(
        name="galaxy",
        url="https://galaxy.ansible.com",
        token_env="GALAXY_TOKEN",
    )
    assert token_only.auth_metadata() == {
        "name": "galaxy",
        "url": "https://galaxy.ansible.com",
        "token_env": "GALAXY_TOKEN",
    }
    assert token_only.generic_bearer_env() == "GALAXY_TOKEN"

    sso = GalaxyServer(
        name="automation_hub",
        url=AH_BASE,
        auth_url=SSO_URL,
        token_env="AUTOMATION_HUB_TOKEN",
        client_id="cloud-services",
    )
    assert sso.auth_metadata() == {
        "name": "automation_hub",
        "url": AH_BASE,
        "token_env": "AUTOMATION_HUB_TOKEN",
        "auth_url": SSO_URL,
        "client_id": "cloud-services",
    }
    assert sso.generic_bearer_env() == DEFAULT_ACCESS_TOKEN_ENV


def test_generic_export_v2_with_sso_server():
    items = [
        CollectionItem(
            name="kubernetes.core",
            version="6.1.0",
            url="https://example.com/kubernetes-core-6.1.0.tar.gz",
            checksum="sha256:abcd",
            size=1,
        )
    ]
    server = GalaxyServer(
        url=AH_BASE,
        auth_url=SSO_URL,
        token_env="AUTOMATION_HUB_TOKEN",
    )
    data = collections_to_generic(items, server=server)
    assert data["metadata"]["version"] == "2.0"
    assert data["artifacts"][0]["auth"]["bearer"]["value"] == (
        f"Bearer ${DEFAULT_ACCESS_TOKEN_ENV}"
    )


def test_generic_export_v2_token_only():
    items = [
        CollectionItem(
            name="community.general",
            version="1.0.0",
            url="https://example.com/x.tar.gz",
            checksum="sha256:ab",
            size=1,
        )
    ]
    server = GalaxyServer(url="https://galaxy.ansible.com", token_env="GALAXY_TOKEN")
    data = collections_to_generic(items, server=server)
    assert data["metadata"]["version"] == "2.0"
    assert data["artifacts"][0]["auth"]["bearer"]["value"] == "Bearer $GALAXY_TOKEN"


@responses.activate
def test_resolve_with_sso_auth(monkeypatch):
    monkeypatch.setenv("AUTOMATION_HUB_TOKEN", "refresh-secret")

    responses.add(
        responses.POST,
        SSO_URL,
        json={"access_token": "access-secret", "expires_in": 900},
    )
    version_url = (
        f"{AH_BASE.rstrip('/')}/v3/plugin/ansible/content/published/collections/"
        "index/kubernetes/core/versions/6.1.0/"
    )
    responses.add(
        responses.GET,
        version_url,
        json={
            "version": "6.1.0",
            "download_url": "https://example.com/kubernetes-core-6.1.0.tar.gz",
            "artifact": {
                "filename": "kubernetes-core-6.1.0.tar.gz",
                "sha256": "33" * 32,
                "size": 50,
            },
            "metadata": {"dependencies": {}},
            "namespace": {"name": "kubernetes"},
            "name": "core",
        },
    )

    server = GalaxyServer(
        url=AH_BASE,
        auth_url=SSO_URL,
        token_env="AUTOMATION_HUB_TOKEN",
    )
    items, _used = resolve_collections(
        [{"name": "kubernetes.core", "version": "6.1.0"}],
        server=server,
    )
    assert len(items) == 1
    assert items[0].name == "kubernetes.core"

    assert responses.calls[0].request.url == SSO_URL
    assert "refresh_token=refresh-secret" in responses.calls[0].request.body
    assert responses.calls[1].request.headers["Authorization"] == "Bearer access-secret"


@responses.activate
def test_resolve_with_api_token(monkeypatch):
    monkeypatch.setenv("GALAXY_TOKEN", "api-key")
    base = "https://galaxy.ansible.com"
    responses.add(
        responses.GET,
        f"{base}/api/v3/plugin/ansible/content/published/collections/index/ns/col/versions/1.0.0/",
        json={
            "version": "1.0.0",
            "download_url": "https://example.com/ns-col-1.0.0.tar.gz",
            "artifact": {
                "filename": "ns-col-1.0.0.tar.gz",
                "sha256": "44" * 32,
                "size": 10,
            },
            "metadata": {"dependencies": {}},
            "namespace": {"name": "ns"},
            "name": "col",
        },
    )
    server = GalaxyServer(url=base, token_env="GALAXY_TOKEN")
    items, _used = resolve_collections(
        [{"name": "ns.col", "version": "1.0.0"}],
        server=server,
    )
    assert items[0].version == "1.0.0"
    assert responses.calls[0].request.headers["Authorization"] == "Token api-key"


def test_missing_token_env_var(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    server = GalaxyServer(
        url=AH_BASE,
        auth_url=SSO_URL,
        token_env="MISSING_TOKEN",
    )
    with pytest.raises(GalaxyError, match="MISSING_TOKEN"):
        resolve_collections(
            [{"name": "kubernetes.core", "version": "6.1.0"}],
            server=server,
        )


@responses.activate
def test_cli_automation_hub_lockfile_auth(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTOMATION_HUB_TOKEN", "refresh-secret")
    monkeypatch.chdir(tmp_path)

    infile = tmp_path / "ansible.in.yaml"
    infile.write_text(
        yaml.safe_dump(
            {
                "contentOrigin": {
                    "galaxy": [
                        {
                            "url": AH_BASE,
                            "auth_url": SSO_URL,
                            "token_env": "AUTOMATION_HUB_TOKEN",
                        }
                    ]
                },
                "collections": [{"name": "kubernetes.core", "version": "6.1.0"}],
            }
        ),
        encoding="utf-8",
    )

    responses.add(
        responses.POST,
        SSO_URL,
        json={"access_token": "access-secret"},
    )
    responses.add(
        responses.GET,
        (
            f"{AH_BASE.rstrip('/')}/v3/plugin/ansible/content/published/collections/"
            "index/kubernetes/core/versions/6.1.0/"
        ),
        json={
            "version": "6.1.0",
            "download_url": "https://example.com/kubernetes-core-6.1.0.tar.gz",
            "artifact": {
                "filename": "kubernetes-core-6.1.0.tar.gz",
                "sha256": "55" * 32,
                "size": 9,
            },
            "metadata": {"dependencies": {}},
            "namespace": {"name": "kubernetes"},
            "name": "core",
        },
    )

    outfile = tmp_path / "ansible.lock.yaml"
    generic = tmp_path / "artifacts.lock.yaml"
    rc = cli_main(
        [
            str(infile),
            f"--outfile={outfile}",
            f"--export-generic={generic}",
        ]
    )
    assert rc == 0
    lock = yaml.safe_load(outfile.read_text(encoding="utf-8"))
    assert lock["auth"]["token_env"] == "AUTOMATION_HUB_TOKEN"
    assert lock["auth"]["auth_url"] == SSO_URL

    generic_data = yaml.safe_load(generic.read_text(encoding="utf-8"))
    assert generic_data["metadata"]["version"] == "2.0"
    assert generic_data["artifacts"][0]["auth"]["bearer"]["value"] == (
        f"Bearer ${DEFAULT_ACCESS_TOKEN_ENV}"
    )


@responses.activate
def test_cli_discovers_cfg_and_requirements(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANSIBLE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    (tmp_path / "ansible.cfg").write_text(
        """
[galaxy]
server_list = galaxy

[galaxy_server.galaxy]
url=https://galaxy.ansible.com
""",
        encoding="utf-8",
    )
    (tmp_path / "requirements.yml").write_text(
        yaml.safe_dump(
            {"collections": [{"name": "community.general", "version": "13.3.0"}]}
        ),
        encoding="utf-8",
    )

    responses.add(
        responses.GET,
        "https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index/community/general/versions/13.3.0/",
        json={
            "version": "13.3.0",
            "download_url": "https://galaxy.ansible.com/dl/community-general-13.3.0.tar.gz",
            "artifact": {
                "filename": "community-general-13.3.0.tar.gz",
                "sha256": "66" * 32,
                "size": 3,
            },
            "metadata": {"dependencies": {}},
            "namespace": {"name": "community"},
            "name": "general",
        },
    )

    outfile = tmp_path / "ansible.lock.yaml"
    rc = cli_main(["--project-dir", str(tmp_path), f"--outfile={outfile}"])
    assert rc == 0
    lock = yaml.safe_load(outfile.read_text(encoding="utf-8"))
    assert lock["collections"][0]["name"] == "community.general"
    assert lock["collections"][0]["server"] == "galaxy"


@responses.activate
def test_cli_custom_requirements_filename(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANSIBLE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    (tmp_path / "ansible.cfg").write_text(
        """
[galaxy]
server_list = galaxy

[galaxy_server.galaxy]
url=https://galaxy.ansible.com
""",
        encoding="utf-8",
    )
    custom = tmp_path / "ee-supported-requirements.yml"
    custom.write_text(
        yaml.safe_dump(
            {"collections": [{"name": "ansible.posix", "version": "1.5.4"}]}
        ),
        encoding="utf-8",
    )

    responses.add(
        responses.GET,
        "https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index/ansible/posix/versions/1.5.4/",
        json={
            "version": "1.5.4",
            "download_url": "https://galaxy.ansible.com/dl/ansible-posix-1.5.4.tar.gz",
            "artifact": {
                "filename": "ansible-posix-1.5.4.tar.gz",
                "sha256": "77" * 32,
                "size": 4,
            },
            "metadata": {"dependencies": {}},
            "namespace": {"name": "ansible"},
            "name": "posix",
        },
    )

    outfile = tmp_path / "ansible.lock.yaml"
    rc = cli_main(
        [str(custom), "--project-dir", str(tmp_path), f"--outfile={outfile}"]
    )
    assert rc == 0
    lock = yaml.safe_load(outfile.read_text(encoding="utf-8"))
    assert lock["collections"][0]["name"] == "ansible.posix"

    # --requirements works the same way
    outfile2 = tmp_path / "ansible2.lock.yaml"
    rc = cli_main(
        [
            "--requirements",
            str(custom),
            "--project-dir",
            str(tmp_path),
            f"--outfile={outfile2}",
        ]
    )
    assert rc == 0
    assert yaml.safe_load(outfile2.read_text(encoding="utf-8"))["collections"][0][
        "name"
    ] == "ansible.posix"
