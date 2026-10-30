"""Deploy only Nicole-owned resources. Credentials never enter the code archive."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from nicole.agentcore import SECRET_KEYS
from nicole.settings import load_env, state_dir

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "nicole-film-demo"
DEPLOYMENT = "nicole-film-agentcore"
QUEUE = "nicole-film-agentcore"
RUNTIME = "nicole_film_demo_worker"


def role(iam, name, trust, permissions):
    try:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[{"Key": "Application", "Value": "Nicole"}],
        )["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(
        RoleName=name, PolicyName="NicoleOnly", PolicyDocument=json.dumps(permissions)
    )
    return arn


def policy(statements):
    return {"Version": "2012-10-17", "Statement": statements}


def allow(actions, resources):
    return {"Effect": "Allow", "Action": actions, "Resource": resources}


def prepare(region, manifest, save):
    session = boto3.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    manifest.update(
        region=region,
        account=account,
        task_queue=QUEUE,
        deployment_name=DEPLOYMENT,
        temporal_address=os.environ["TEMPORAL_ADDRESS"],
        temporal_namespace=os.environ["TEMPORAL_NAMESPACE"],
    )
    ddb = session.client("dynamodb")
    try:
        ddb.describe_table(TableName=PREFIX)
    except ddb.exceptions.ResourceNotFoundException:
        ddb.create_table(
            TableName=PREFIX,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": k, "AttributeType": "S"} for k in ["PK", "SK"]
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            Tags=[{"Key": "Application", "Value": "Nicole"}],
        )
    manifest["table_name"] = PREFIX
    save()
    print("Shared DynamoDB table prepared", flush=True)
    bucket = f"{PREFIX}-{account}-{region}"
    s3 = session.client("s3")
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        s3.create_bucket(
            Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region}
        )
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]
        },
    )
    manifest["bucket"] = bucket
    payload_bucket = f"{PREFIX}-payloads-{account}-{region}"
    try:
        s3.head_bucket(Bucket=payload_bucket, ExpectedBucketOwner=account)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        s3.create_bucket(
            Bucket=payload_bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    s3.put_public_access_block(
        Bucket=payload_bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=payload_bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]
        },
    )
    s3.put_bucket_versioning(
        Bucket=payload_bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    s3.put_bucket_tagging(
        Bucket=payload_bucket,
        Tagging={"TagSet": [{"Key": "Application", "Value": "Nicole"}]},
    )
    # No expiry: claims must remain readable throughout execution AND retention.
    manifest["external_storage"] = {
        "bucket": payload_bucket,
        "owner": account,
        "region": region,
        "threshold": 16 * 1024,
    }
    save()
    print(
        "Private payload bucket prepared (16 KiB demo threshold; no expiry)", flush=True
    )
    secrets = session.client("secretsmanager")
    values = {k: os.environ[k] for k in SECRET_KEYS if os.getenv(k)}
    for required in ("TEMPORAL_API_KEY", "OMDB_API_KEY", "OPENROUTER_API_KEY"):
        if not values.get(required):
            raise ValueError("Missing credential: " + required)
    try:
        r = secrets.create_secret(
            Name="nicole/film-demo/providers",
            SecretString=json.dumps(values),
            Tags=[{"Key": "Application", "Value": "Nicole"}],
        )
    except secrets.exceptions.ResourceExistsException:
        r = secrets.put_secret_value(
            SecretId="nicole/film-demo/providers", SecretString=json.dumps(values)
        )
    manifest["secret_arn"] = r["ARN"]
    save()
    print("Provider credentials stored in Secrets Manager (values omitted)", flush=True)
    iam = session.client("iam")
    trust = policy(
        [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ]
    )
    memory_region = region
    manifest["memory_region"] = memory_region
    control = session.client("bedrock-agentcore-control", region_name=memory_region)
    if not manifest.get("memory_id"):
        memory = control.create_memory(
            name="NicoleFilmPreferences",
            description="Film taste across Nicole visits",
            eventExpiryDuration=30,
            memoryStrategies=[
                {
                    "userPreferenceMemoryStrategy": {
                        "name": "FilmTaste",
                        "description": "Lasting film taste and favorites",
                        "namespaceTemplates": ["/nicole/{actorId}/preferences/"],
                    }
                }
            ],
            tags={"Application": "Nicole"},
        )["memory"]
        manifest["memory_id"] = memory["id"]
        manifest["memory_arn"] = memory["arn"]
        manifest["memory_strategy"] = "built-in user preference"
        save()
    print("AgentCore film-preference memory provisioned", flush=True)
    runtime_role = role(
        iam,
        PREFIX + "-runtime",
        trust,
        policy(
            [
                allow(["s3:GetObject"], f"arn:aws:s3:::{bucket}/code/*"),
                allow(["s3:ListBucket"], f"arn:aws:s3:::{bucket}"),
                allow(
                    ["s3:GetObject", "s3:PutObject"],
                    f"arn:aws:s3:::{payload_bucket}/v0/*",
                ),
                allow(["s3:ListBucket"], f"arn:aws:s3:::{payload_bucket}"),
                allow(["secretsmanager:GetSecretValue"], manifest["secret_arn"]),
                allow(
                    [
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:Query",
                        "dynamodb:ConditionCheckItem",
                    ],
                    f"arn:aws:dynamodb:{region}:{account}:table/{PREFIX}",
                ),
                allow(
                    [
                        "bedrock-agentcore:CreateEvent",
                        "bedrock-agentcore:RetrieveMemoryRecords",
                        "bedrock-agentcore:ListMemoryRecords",
                        "bedrock-agentcore:GetMemoryRecord",
                    ],
                    manifest["memory_arn"],
                ),
                allow(
                    [
                        "logs:CreateLogGroup",
                        "logs:DescribeLogStreams",
                        "logs:PutResourcePolicy",
                    ],
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{RUNTIME}-*",
                ),
                allow(
                    ["logs:CreateLogStream", "logs:PutLogEvents"],
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{RUNTIME}-*:log-stream:*",
                ),
                allow(
                    ["logs:DescribeLogGroups"],
                    f"arn:aws:logs:{region}:{account}:log-group:*",
                ),
            ]
        ),
    )
    manifest["runtime_role_arn"] = runtime_role
    save()
    print("Nicole Runtime role prepared", flush=True)


def package(manifest, save):
    build = state_dir() / "agentcore-build"
    wheels = build / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=ROOT, check=True
    )
    wheel = sorted(wheels.glob("*.whl"), key=lambda p: p.stat().st_mtime)[-1]
    target = build / "package"
    if target.exists():
        shutil.rmtree(target)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python-platform",
            "aarch64-manylinux_2_28",
            "--python-version",
            "3.12",
            "--only-binary=:all:",
            "--target",
            str(target),
            str(wheel) + "[agentcore]",
        ],
        cwd=ROOT,
        check=True,
    )
    shutil.copy2(ROOT / "agentcore_worker.py", target / "agentcore_worker.py")
    archive = build / "nicole.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(target.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                if p.name == ".env":
                    raise ValueError("Refusing to package credentials")
                info = zipfile.ZipInfo(str(p.relative_to(target)))
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, p.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest["build_id"] = "v" + digest[:12]
    manifest["code_key"] = f"code/{digest}/nicole.zip"
    boto3.client("s3", region_name=manifest["region"]).upload_file(
        str(archive),
        manifest["bucket"],
        manifest["code_key"],
        ExtraArgs={"ExpectedBucketOwner": manifest["account"]},
    )
    save()
    print(
        f"Packaged Linux ARM64 Worker ({archive.stat().st_size // 1024 // 1024} MiB); "
        "credentials excluded",
        flush=True,
    )


def deploy(manifest, save):
    region = manifest["region"]
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    params = dict(
        agentRuntimeArtifact={
            "codeConfiguration": {
                "code": {
                    "s3": {"bucket": manifest["bucket"], "prefix": manifest["code_key"]}
                },
                "runtime": "PYTHON_3_12",
                "entryPoint": ["agentcore_worker.py"],
            }
        },
        roleArn=manifest["runtime_role_arn"],
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 300, "maxLifetime": 1800},
        environmentVariables={
            "AWS_REGION": region,
            "NICOLE_SKIP_DOTENV": "1",
            "TEMPORAL_MODE": "cloud",
            "TEMPORAL_ADDRESS": manifest["temporal_address"],
            "TEMPORAL_NAMESPACE": manifest["temporal_namespace"],
            "NICOLE_TASK_QUEUE": QUEUE,
            "NICOLE_DEPLOYMENT_NAME": DEPLOYMENT,
            "NICOLE_BUILD_ID": manifest["build_id"],
            "NICOLE_DYNAMODB_TABLE": manifest["table_name"],
            "NICOLE_SECRET_ARN": manifest["secret_arn"],
            "AGENTCORE_MEMORY_ID": manifest["memory_id"],
            "AGENTCORE_MEMORY_REGION": manifest["memory_region"],
            "AGENTCORE_MEMORY_NAMESPACE": "/nicole/{actorId}/preferences/",
            "AGENTCORE_MEMORY_INPUT": "facts",
            "NICOLE_IDLE_SECONDS": "60",
            "NICOLE_PAYLOAD_BUCKET": manifest["external_storage"]["bucket"],
            "NICOLE_PAYLOAD_REGION": manifest["external_storage"]["region"],
            "NICOLE_PAYLOAD_OWNER": manifest["external_storage"]["owner"],
            "NICOLE_PAYLOAD_THRESHOLD": str(manifest["external_storage"]["threshold"]),
        },
    )
    if manifest.get("runtime_id"):
        r = control.get_agent_runtime(agentRuntimeId=manifest["runtime_id"])
        if (
            r["agentRuntimeArtifact"] != params["agentRuntimeArtifact"]
            or r.get("environmentVariables") != params["environmentVariables"]
        ):
            r = control.update_agent_runtime(
                agentRuntimeId=manifest["runtime_id"], **params
            )
    else:
        r = control.create_agent_runtime(
            agentRuntimeName=RUNTIME, **params, tags={"Application": "Nicole"}
        )
    manifest.update(
        runtime_id=r["agentRuntimeId"],
        runtime_arn=r["agentRuntimeArn"],
        runtime_version=r["agentRuntimeVersion"],
    )
    save()
    for _ in range(120):
        r = control.get_agent_runtime(agentRuntimeId=manifest["runtime_id"])
        if r["status"] == "READY":
            break
        if r["status"] in ("CREATE_FAILED", "UPDATE_FAILED"):
            raise RuntimeError(r.get("failureReason", "Runtime deployment failed"))
        time.sleep(5)
    else:
        raise RuntimeError("Runtime is still deploying; inspect it before continuing")
    endpoint = "temporal_" + manifest["build_id"]
    try:
        r = control.get_agent_runtime_endpoint(
            agentRuntimeId=manifest["runtime_id"], endpointName=endpoint
        )
        if r["targetVersion"] != manifest["runtime_version"]:
            raise ValueError("An immutable endpoint already targets another version")
    except control.exceptions.ResourceNotFoundException:
        r = control.create_agent_runtime_endpoint(
            agentRuntimeId=manifest["runtime_id"],
            name=endpoint,
            agentRuntimeVersion=manifest["runtime_version"],
        )
    manifest.update(endpoint_name=endpoint, endpoint_arn=r["agentRuntimeEndpointArn"])
    save()
    print("AgentCore Runtime is READY; immutable-version endpoint created", flush=True)
    template = (ROOT / "deploy/temporal-invoke-role.yaml").read_text()
    cf = boto3.client("cloudformation", region_name=region)
    stack = PREFIX + "-invoke"
    if not manifest.get("invoke_role_arn"):
        cf.create_stack(
            StackName=stack,
            TemplateBody=template,
            Parameters=[
                {
                    "ParameterKey": "AssumeRoleExternalId",
                    "ParameterValue": PREFIX + "-" + manifest["account"],
                },
                {
                    "ParameterKey": "AgentRuntimeARNs",
                    "ParameterValue": manifest["runtime_arn"] + "*",
                },
                {"ParameterKey": "RoleName", "ParameterValue": "nicole-temporal"},
            ],
            Capabilities=["CAPABILITY_NAMED_IAM"],
            Tags=[{"Key": "Application", "Value": "Nicole"}],
        )
        cf.get_waiter("stack_create_complete").wait(
            StackName=stack, WaiterConfig={"Delay": 5, "MaxAttempts": 120}
        )
        outputs = cf.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
        manifest["invoke_role_arn"] = next(
            x["OutputValue"] for x in outputs if x["OutputKey"] == "RoleARN"
        )
        save()
    manifest["external_id"] = PREFIX + "-" + manifest["account"]
    save()


def register(manifest, save):
    env = dict(
        os.environ,
        TEMPORAL_ADDRESS=manifest["temporal_address"],
        TEMPORAL_NAMESPACE=manifest["temporal_namespace"],
        TEMPORAL_TLS="true",
    )

    def temporal(*args, allow_exists=False):
        r = subprocess.run(["temporal", *args], env=env, capture_output=True, text=True)
        if r.returncode and not (allow_exists and "already exists" in r.stderr.lower()):
            raise RuntimeError(r.stderr.strip())

    temporal("worker", "deployment", "create", "--name", DEPLOYMENT, allow_exists=True)
    temporal(
        "worker",
        "deployment",
        "create-version",
        "--deployment-name",
        DEPLOYMENT,
        "--build-id",
        manifest["build_id"],
        "--aws-agentcore-endpoint-arn",
        manifest["endpoint_arn"],
        "--aws-agentcore-assume-role-arn",
        manifest["invoke_role_arn"],
        "--aws-agentcore-assume-role-external-id",
        manifest["external_id"],
        allow_exists=True,
    )
    temporal(
        "worker",
        "deployment",
        "set-current-version",
        "--deployment-name",
        DEPLOYMENT,
        "--build-id",
        manifest["build_id"],
        "--yes",
    )
    manifest["registered"] = True
    save()
    print("Temporal Worker Deployment Version registered and current", flush=True)


def main():
    load_env()
    args = argparse.ArgumentParser()
    args.add_argument("phase", choices=["prepare", "package", "deploy", "register"])
    args.add_argument("--region", default="us-east-2")
    selected = args.parse_args()
    path = state_dir() / "agentcore-deployment.json"
    manifest = json.loads(path.read_text()) if path.exists() else {}

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n")

    if selected.phase == "prepare":
        prepare(selected.region, manifest, save)
    else:
        globals()[selected.phase](manifest, save)


if __name__ == "__main__":
    main()
