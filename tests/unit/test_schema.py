from __future__ import annotations

import json

import pytest
from jsonschema import ValidationError

from ansible_lockfile import schema


def test_print_schema(capsys):
    schema.print_schema()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["type"] == "object"
    assert "collections" in data["properties"]


def test_valid_inline_collections():
    schema.validate(
        {
            "collections": [
                {"name": "community.general", "version": "13.3.0"},
            ]
        }
    )


def test_valid_requirements_file_only():
    schema.validate({"requirementsFile": "requirements.yml"})


def test_rejects_missing_collections_and_requirements():
    with pytest.raises(ValidationError):
        schema.validate({"contentOrigin": {"galaxy": [{"url": "https://galaxy.ansible.com"}]}})


def test_rejects_invalid_collection_name():
    with pytest.raises(ValidationError):
        schema.validate({"collections": [{"name": "noperiod"}]})


def test_rejects_extra_collection_keys():
    with pytest.raises(ValidationError):
        schema.validate(
            {
                "collections": [
                    {"name": "community.general", "type": "git"},
                ]
            }
        )


def test_valid_automation_hub_origin():
    schema.validate(
        {
            "contentOrigin": {
                "galaxy": [
                    {
                        "url": "https://console.redhat.com/api/automation-hub/",
                        "auth_url": (
                            "https://sso.redhat.com/auth/realms/redhat-external/"
                            "protocol/openid-connect/token"
                        ),
                        "token_env": "AUTOMATION_HUB_TOKEN",
                        "client_id": "cloud-services",
                    }
                ]
            },
            "collections": [{"name": "kubernetes.core", "version": "6.1.0"}],
        }
    )


def test_rejects_auth_url_without_token_env():
    with pytest.raises(ValidationError):
        schema.validate(
            {
                "contentOrigin": {
                    "galaxy": [
                        {
                            "url": "https://console.redhat.com/api/automation-hub/",
                            "auth_url": (
                                "https://sso.redhat.com/auth/realms/redhat-external/"
                                "protocol/openid-connect/token"
                            ),
                        }
                    ]
                },
                "collections": [{"name": "kubernetes.core"}],
            }
        )


def test_valid_token_env_only():
    schema.validate(
        {
            "contentOrigin": {
                "galaxy": [
                    {
                        "url": "https://galaxy.ansible.com",
                        "token_env": "GALAXY_TOKEN",
                    }
                ]
            },
            "collections": [{"name": "community.general"}],
        }
    )
