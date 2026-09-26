"""Human passwords in AWS Parameter Store: <aws_ssm_prefix><role>."""

from __future__ import annotations

import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import SsmSettings


def _client(settings: SsmSettings):
    return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region).client("ssm")


def _prefix(settings: SsmSettings) -> str:
    return "/" + settings.aws_ssm_prefix.strip("/") + "/"


def ssm_users(settings: SsmSettings) -> set[str]:
    """Role names with a password parameter (metadata only, values not read)."""
    prefix = _prefix(settings)
    try:
        ssm = _client(settings)
        names = set()
        for page in ssm.get_paginator("describe_parameters").paginate(
            ParameterFilters=[{"Key": "Path", "Option": "OneLevel", "Values": [prefix.rstrip("/")]}]
        ):
            names |= {p["Name"][len(prefix):] for p in page["Parameters"]}
        return names
    except (BotoCoreError, ClientError) as exc:
        sys.exit(f"error listing SSM parameters under {prefix} ({settings.aws_profile}, {settings.aws_region}): {exc}")


def ssm_passwords(settings: SsmSettings, names: list[str]) -> dict[str, str]:
    """name -> password for the names that have a parameter."""
    prefix = _prefix(settings)
    out: dict[str, str] = {}
    try:
        ssm = _client(settings)
        for i in range(0, len(names), 10):  # GetParameters takes at most 10 names
            resp = ssm.get_parameters(Names=[prefix + n for n in names[i:i + 10]], WithDecryption=True)
            out |= {p["Name"][len(prefix):]: p["Value"] for p in resp["Parameters"]}
        return out
    except (BotoCoreError, ClientError) as exc:
        sys.exit(f"error reading SSM parameters under {prefix} ({settings.aws_profile}, {settings.aws_region}): {exc}")


def ssm_create_password(settings: SsmSettings, name: str, password: str) -> str:
    """Store a new SecureString <prefix><name> (never overwrites); returns its name."""
    param = _prefix(settings) + name
    try:
        _client(settings).put_parameter(Name=param, Type="SecureString", Value=password, Overwrite=False)
        return param
    except (BotoCoreError, ClientError) as exc:
        sys.exit(f"error creating SSM parameter {param} ({settings.aws_profile}, {settings.aws_region}): {exc}")
