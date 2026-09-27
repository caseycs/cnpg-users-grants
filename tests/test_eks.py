import base64
from urllib.parse import parse_qs, urlparse

import boto3

from cnpg_users.eks import eks_token, parse_get_token

ARGS = ["aws", "--region", "eu-central-1", "eks", "get-token", "--cluster-name", "my-cluster", "--output", "json"]


def test_parse_kubeconfig_exec_args():
    assert parse_get_token(ARGS, {"AWS_PROFILE": "my-profile"}) == ("my-cluster", "eu-central-1", "my-profile")
    assert parse_get_token(["/usr/local/bin/aws", "eks", "get-token", "--cluster-name=c", "--profile", "p"],
                           {"AWS_DEFAULT_REGION": "us-east-1"}) == ("c", "us-east-1", "p")


def test_other_exec_plugins_fall_back():
    assert parse_get_token(["kubelogin", "get-token"], {}) is None
    assert parse_get_token(["aws", "eks", "update-kubeconfig", "--name", "c"], {}) is None
    assert parse_get_token(ARGS + ["--role-arn", "arn:aws:iam::1:role/x"], {}) is None  # not implemented here
    assert parse_get_token(["aws", "eks", "get-token"], {}) is None  # no cluster name


def test_eks_token_is_presigned_get_caller_identity():
    session = boto3.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret", region_name="eu-central-1")
    token = eks_token(session, "my-cluster", "eu-central-1")
    assert token.startswith("k8s-aws-v1.") and "=" not in token
    b64 = token[len("k8s-aws-v1."):]
    url = urlparse(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode())
    query = parse_qs(url.query)
    assert url.netloc == "sts.eu-central-1.amazonaws.com"
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-Expires"] == ["60"]
    assert "x-k8s-aws-id" in query["X-Amz-SignedHeaders"][0].split(";")
