from __future__ import annotations

import json
import sys

import jsonschema


def get_schema() -> dict:
    galaxy_server = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "minLength": 1,
            },
            "auth_url": {
                "type": "string",
                "minLength": 1,
            },
            "token_env": {
                "type": "string",
                "minLength": 1,
            },
            "client_id": {
                "type": "string",
                "minLength": 1,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
        # auth_url requires token_env (refresh token lives in the env var).
        "dependencies": {
            "auth_url": ["token_env"],
        },
    }
    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "properties": {
            "contentOrigin": {
                "type": "object",
                "properties": {
                    "galaxy": {
                        "type": "array",
                        "minItems": 1,
                        "items": galaxy_server,
                    },
                },
                "additionalProperties": False,
            },
            "collections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "pattern": r"^[^.]+\.[^.]+$",
                        },
                        "version": {
                            "type": "string",
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "requirementsFile": {
                "type": "string",
                "minLength": 1,
            },
        },
        "additionalProperties": False,
        "anyOf": [
            {"required": ["collections"]},
            {"required": ["requirementsFile"]},
        ],
    }


def validate(data: dict) -> None:
    jsonschema.validate(data, get_schema())


def print_schema() -> None:
    json.dump(get_schema(), sys.stdout, indent=2)
    sys.stdout.write("\n")
