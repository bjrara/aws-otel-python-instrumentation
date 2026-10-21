# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Creates and destroys the throwaway Lambda stack a benchmark run needs.

Every resource is named with the run prefix and tagged with the run id, and every delete asserts the
prefix before it fires. The account this runs in is a shared development account with unrelated stacks
and canaries in it, so teardown must never be able to reach anything it did not create.

No API Gateway and no public endpoint: the function is driven by lambda:Invoke, so nothing here is
reachable from outside the account.
"""
import io
import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "us-east-1"  # release management and the agent space are us-east-1 only
NAME_PREFIX = "udpbench"
TAG_KEY = "benchmark-run"

_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
    ],
}
_MANAGED_POLICIES = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess",
)
_HANDLER = "benchmark_handler.lambda_handler"
_HANDLER_SOURCE = "lambda-layer/sample-apps/function/benchmark_handler.py"
# CreateFunction rejects a role IAM has not finished propagating; this is the documented workaround
_ROLE_PROPAGATION_ATTEMPTS = 20
_ROLE_PROPAGATION_SLEEP_S = 3


class StackError(RuntimeError):
    pass


@dataclass
class Stack:
    run_id: str
    tag: str
    function_name: str
    role_name: str
    role_arn: str
    layer_arn: str
    log_group: str

    @property
    def owns_prefix(self) -> str:
        return f"{NAME_PREFIX}-{self.run_id}-{self.tag}"


def _clients():
    config = Config(region_name=REGION, retries={"max_attempts": 5, "mode": "standard"})
    return boto3.client("lambda", config=config), boto3.client("iam", config=config), boto3.client(
        "logs", config=config
    )


def _assert_ours(name: str, run_id: str) -> None:
    """Refuse to delete anything that is not this run's."""
    if not name.startswith(f"{NAME_PREFIX}-{run_id}-"):
        raise StackError(f"refusing to operate on {name!r}: not created by run {run_id}")


def _handler_zip(repo_root: Path) -> bytes:
    source = repo_root / _HANDLER_SOURCE
    if not source.exists():
        raise StackError(f"{source} missing")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("benchmark_handler.py", source.read_text())
    return buffer.getvalue()


def create(
    run_id: str,
    tag: str,
    layer_zip: Path,
    repo_root: Path,
    memory_mb: int = 512,
    runtime: str = "python3.13",
    architecture: str = "x86_64",
    lite_mode: bool = True,
    timeout_s: int = 60,
) -> Stack:
    """Stand up role, layer and function. Caller is responsible for calling destroy()."""
    lambda_client, iam, _ = _clients()
    base = f"{NAME_PREFIX}-{run_id}-{tag}"
    role_name = f"{base}-role"
    function_name = f"{base}-fn"
    tags = {TAG_KEY: run_id, "benchmark-ref": tag}

    role_arn = _ensure_role(iam, role_name, tags)
    layer_arn = _publish_layer(lambda_client, f"{base}-layer", layer_zip, runtime, architecture)

    environment = {
        "AWS_LAMBDA_EXEC_WRAPPER": "/opt/otel-instrument",
        "OTEL_AWS_APPLICATION_SIGNALS_ENABLED": "true",
        # Runtime metrics are meaningless on Lambda; see benchmarks/GAPS.md B1
        "OTEL_AWS_APPLICATION_SIGNALS_RUNTIME_ENABLED": "false",
        "OTEL_SERVICE_NAME": function_name,
    }
    if lite_mode:
        environment["OTEL_AWS_LAMBDA_FAST_START"] = "true"

    last_error: Optional[Exception] = None
    for _ in range(_ROLE_PROPAGATION_ATTEMPTS):
        try:
            lambda_client.create_function(
                FunctionName=function_name,
                Runtime=runtime,
                Role=role_arn,
                Handler=_HANDLER,
                Code={"ZipFile": _handler_zip(repo_root)},
                MemorySize=memory_mb,
                Timeout=timeout_s,
                Layers=[layer_arn],
                Architectures=[architecture],
                TracingConfig={"Mode": "Active"},  # starts the X-Ray daemon that receives the UDP spans
                Environment={"Variables": environment},
                Tags=tags,
            )
            break
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvalidParameterValueException":
                raise
            last_error = exc
            time.sleep(_ROLE_PROPAGATION_SLEEP_S)
    else:
        raise StackError(f"create_function kept failing while the role propagated: {last_error}")

    lambda_client.get_waiter("function_active_v2").wait(FunctionName=function_name)
    return Stack(
        run_id=run_id,
        tag=tag,
        function_name=function_name,
        role_name=role_name,
        role_arn=role_arn,
        layer_arn=layer_arn,
        log_group=f"/aws/lambda/{function_name}",
    )


def _ensure_role(iam, role_name: str, tags: Dict[str, str]) -> str:
    try:
        created = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(_TRUST_POLICY),
            Description="Throwaway role for a PR benchmark run",
            Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )
        arn = created["Role"]["Arn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    for policy in _MANAGED_POLICIES:
        iam.attach_role_policy(RoleName=role_name, PolicyArn=policy)
    return arn


def _publish_layer(lambda_client, layer_name: str, layer_zip: Path, runtime: str, architecture: str) -> str:
    published = lambda_client.publish_layer_version(
        LayerName=layer_name,
        Content={"ZipFile": Path(layer_zip).read_bytes()},
        CompatibleRuntimes=[runtime],
        CompatibleArchitectures=[architecture],
        Description="Throwaway layer for a PR benchmark run",
    )
    return published["LayerVersionArn"]


def destroy(stack: Stack) -> List[str]:
    """Delete everything create() made, in reverse order. Returns what was removed."""
    lambda_client, iam, logs = _clients()
    removed = []

    _assert_ours(stack.function_name, stack.run_id)
    for name, call in (
        (stack.function_name, lambda: lambda_client.delete_function(FunctionName=stack.function_name)),
        (stack.log_group, lambda: logs.delete_log_group(logGroupName=stack.log_group)),
    ):
        try:
            call()
            removed.append(name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("ResourceNotFoundException", "ValidationException"):
                raise

    layer_name = stack.layer_arn.split(":layer:")[1].split(":")[0]
    _assert_ours(layer_name, stack.run_id)
    try:
        versions = lambda_client.list_layer_versions(LayerName=layer_name)["LayerVersions"]
        for version in versions:
            lambda_client.delete_layer_version(LayerName=layer_name, VersionNumber=version["Version"])
        removed.append(f"{layer_name} ({len(versions)} version(s))")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    _assert_ours(stack.role_name, stack.run_id)
    try:
        for policy in iam.list_attached_role_policies(RoleName=stack.role_name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=stack.role_name, PolicyArn=policy["PolicyArn"])
        iam.delete_role(RoleName=stack.role_name)
        removed.append(stack.role_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise

    return removed


def find_orphans(run_id: str = "") -> Dict[str, List[str]]:
    """List leftover benchmark resources, so an interrupted run can be cleaned up explicitly."""
    lambda_client, iam, _ = _clients()
    prefix = f"{NAME_PREFIX}-{run_id}" if run_id else NAME_PREFIX
    functions = [
        f["FunctionName"]
        for page in lambda_client.get_paginator("list_functions").paginate()
        for f in page["Functions"]
        if f["FunctionName"].startswith(prefix)
    ]
    layers = [
        layer["LayerName"]
        for page in lambda_client.get_paginator("list_layers").paginate()
        for layer in page["Layers"]
        if layer["LayerName"].startswith(prefix)
    ]
    roles = [
        role["RoleName"]
        for page in iam.get_paginator("list_roles").paginate()
        for role in page["Roles"]
        if role["RoleName"].startswith(prefix)
    ]
    return {"functions": functions, "layers": layers, "roles": roles}
