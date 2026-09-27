"""EKS tokens in-process instead of running `aws eks get-token`.

kubeconfigs for EKS log in through an exec plugin (`aws --region R eks
get-token --cluster-name C`, often with AWS_PROFILE in its env). The
kubernetes client runs that command per connection: ~1s of process start
each, and it shares the terminal, so Ctrl+C makes it print its own traceback.
An EKS token is just a presigned STS GetCallerIdentity URL carrying the
cluster name in a signed header, so it is built here with boto3; any other
exec plugin still runs as configured."""

from __future__ import annotations

import base64
import datetime
import os
import threading

import boto3
from kubernetes.config import kube_config
from kubernetes.config.exec_provider import ExecProvider

TOKEN_PREFIX = "k8s-aws-v1."
PRESIGN_SECONDS = 60      # as `aws eks get-token`
TOKEN_LIFETIME_MINUTES = 14  # EKS accepts 15 min from signing; the client refreshes after this

_sessions: dict[tuple[str | None, str | None], boto3.Session] = {}
_sessions_lock = threading.Lock()


def eks_token(session: boto3.Session, cluster_name: str, region: str) -> str:
    sts = session.client("sts", region_name=region, endpoint_url=f"https://sts.{region}.amazonaws.com")

    def add_cluster_header(request, **kwargs):
        request.headers["x-k8s-aws-id"] = cluster_name

    sts.meta.events.register("before-sign.sts.GetCallerIdentity", add_cluster_header)
    url = sts.generate_presigned_url("get_caller_identity", Params={}, ExpiresIn=PRESIGN_SECONDS, HttpMethod="GET")
    return TOKEN_PREFIX + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def parse_get_token(args: list[str], env: dict[str, str]) -> tuple[str, str | None, str | None] | None:
    """(cluster name, region, profile) for `aws ... eks get-token ...`, or None
    for any other command, or one using options this doesn't implement."""
    if not args or os.path.basename(args[0]) != "aws":
        return None
    opts, positional, rest = {}, [], args[1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg.startswith("--"):
            key, _, value = arg[2:].partition("=")
            if not value and i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                value, i = rest[i + 1], i + 1
            opts[key] = value
        else:
            positional.append(arg)
        i += 1
    if positional != ["eks", "get-token"] or "cluster-name" not in opts:
        return None
    if set(opts) - {"cluster-name", "region", "profile", "output"}:  # e.g. --role-arn, --cluster-id
        return None
    region = opts.get("region") or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    return opts["cluster-name"], region, opts.get("profile") or env.get("AWS_PROFILE")


def _session(profile: str | None, region: str | None) -> boto3.Session:
    with _sessions_lock:  # one per profile: credentials (SSO, assume-role) resolved once
        if (profile, region) not in _sessions:
            _sessions[(profile, region)] = boto3.Session(profile_name=profile, region_name=region)
        return _sessions[(profile, region)]


class EksExecProvider(ExecProvider):
    def run(self, previous_response=None):
        parsed = parse_get_token(self.args, self.env)
        if parsed is None:
            return super().run(previous_response)
        cluster_name, region, profile = parsed
        session = _session(profile, region)
        region = region or session.region_name
        expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=TOKEN_LIFETIME_MINUTES)
        return {"token": eks_token(session, cluster_name, region),
                "expirationTimestamp": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}


def install() -> None:
    """Make the kubernetes client's kubeconfig loader use EksExecProvider."""
    kube_config.ExecProvider = EksExecProvider
