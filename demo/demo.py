#!/usr/bin/env python3
"""Step-by-step: which VPC endpoints does an AgentCore Runtime (direct code deploy) need in private subnets with no NAT?

Commands:
  all       reset to baseline, create the runtime, run steps S0..S5 (adds endpoints each step, probes after each), print the summary
  destroy   delete the runtime, then the stack, timing how long the network interfaces take to release
The stack (VPC, roles, bucket, endpoints) is CDK; the runtime is created through the API because CloudFormation cannot set requireServiceS3Endpoint.
"""
import json, os, subprocess, sys, time, uuid
import boto3
from botocore.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDK = os.path.join(ROOT, "cdk")
REGION = "us-east-1"
NAME = "vpcprobe"
ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
rt = boto3.client("bedrock-agentcore", region_name=REGION, config=Config(read_timeout=180, retries={"max_attempts": 0}))
logs = boto3.client("logs", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)

STEPS = [  # (label, s3 gateway endpoint, interface endpoints)
    ("S0 baseline: no endpoints", False, []),
    ("S1 + S3 gateway endpoint", True, []),
    ("S2 + logs", True, ["logs"]),
    ("S3 + sts", True, ["logs", "sts"]),
    ("S4 + bedrock-runtime, monitoring, xray", True, ["logs", "sts", "bedrock-runtime", "monitoring", "xray"]),
    ("S5 + ecr.api, ce, email, rds, elasticloadbalancing, kafka (no private DNS)", True,
     ["logs", "sts", "bedrock-runtime", "monitoring", "xray", "ecr.api", "ce", "email", "rds", "elasticloadbalancing", "kafka:nopd"]),
]


def say(m=""): print(m, flush=True)


def cdk(args, capture=False):
    env = dict(os.environ, AWS_REGION=REGION, DEMO_REGION=REGION)
    creds = subprocess.run(["aws", "configure", "export-credentials", "--format", "env"], capture_output=True, text=True).stdout
    for line in creds.splitlines():
        if line.startswith("export "):
            k, _, v = line[7:].partition("="); env[k] = v
    env.setdefault("CDK_DEFAULT_ACCOUNT", boto3.client("sts").get_caller_identity()["Account"])
    r = subprocess.run(["npx", "cdk"] + args, cwd=CDK, env=env, capture_output=True, text=True)
    return r


def outputs():
    return json.load(open(os.path.join(CDK, "cdk.outputs.json")))["PrivateSubnetProbe"]


def deploy(s3gw, eps):
    t0 = time.time()
    r = cdk(["deploy", "--require-approval", "never", "--outputs-file", "cdk.outputs.json", "-c", f"endpoints={','.join(eps)}", "-c", f"s3gw={'true' if s3gw else 'false'}"])
    if r.returncode != 0: say(r.stderr[-1500:]); sys.exit(1)
    say(f"  stack deployed in {int(time.time()-t0)}s")


def find_runtime():
    for p in ctl.get_paginator("list_agent_runtimes").paginate():
        for r in p["agentRuntimes"]:
            if r["agentRuntimeName"] == NAME: return r["agentRuntimeId"]


def ensure_runtime(O):
    rid = find_runtime()
    if rid: say(f"runtime exists: {rid}"); return rid
    boto3.client("s3", region_name=REGION).upload_file(os.path.join(ROOT, "probe.zip"), O["CodeBucket"], "probe/app.zip")
    t0 = time.time()
    r = ctl.create_agent_runtime(agentRuntimeName=NAME,
        agentRuntimeArtifact={"codeConfiguration": {"code": {"s3": {"bucket": O["CodeBucket"], "prefix": "probe/app.zip"}}, "runtime": "PYTHON_3_13", "entryPoint": ["main.py"]}},
        roleArn=O["ExecRoleArn"],
        networkConfiguration={"networkMode": "VPC", "networkModeConfig": {"subnets": O["SubnetIds"].split(","), "securityGroups": [O["RuntimeSgId"]]}},
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables={"PROBE_BUCKET": O["CodeBucket"], "PROBE_TARGET_ROLE_ARN": O["TargetRoleArn"]},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 60, "maxLifetime": 900})
    rid = r["agentRuntimeId"]
    while ctl.get_agent_runtime(agentRuntimeId=rid)["status"] in ("CREATING", "UPDATING"): time.sleep(10)
    say(f"runtime {rid} READY after {int(time.time()-t0)}s (created with no endpoints at all)")
    return rid


def invoke(rid, tag):
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{boto3.client('sts').get_caller_identity()['Account']}:runtime/{rid}"
    for attempt in (1, 2):
        sid = f"{tag}-{uuid.uuid4().hex}-session"; t0 = time.time()
        try:
            r = rt.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sid, payload=json.dumps({"input": {}}).encode())
            return ("ok", int(time.time() - t0), json.loads(r["response"].read().decode()), attempt)
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:160]}"
            if attempt == 2 or "initialization time exceeded" not in str(e): return ("error", int(time.time() - t0), err, attempt)
            say(f"  (attempt 1 failed after {int(time.time()-t0)}s: {err}; retrying once on a new session)")


def wait_endpoints(O):
    """Wait until every endpoint in the VPC is 'available' (or stuck in a terminal/pending state), return {service: state}."""
    t0 = time.time()
    while True:
        eps = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [O["VpcId"]]}])["VpcEndpoints"]
        states = {e["ServiceName"].split(f"{REGION}.")[1]: e["State"] for e in eps}
        if all(v in ("available", "pendingacceptance", "rejected", "failed") for v in states.values()) or time.time() - t0 > 900: break
        time.sleep(10)
    say(f"  endpoint states after {int(time.time()-t0)}s: {states}")
    return states


def stable_probe(rid, tag, max_wait=600):
    """Probe repeatedly on ONE warm session until the reachable set is unchanged for 3 probes. Returns (final results, seconds to stable, first-seen map)."""
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{boto3.client('sts').get_caller_identity()['Account']}:runtime/{rid}"
    sid = f"{tag}-{uuid.uuid4().hex}-session"; t0 = time.time(); first = {}; last = None; same = 0; res = None; last_change = 0
    while time.time() - t0 < max_wait:
        try:
            r = rt.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sid, payload=json.dumps({"input": {}}).encode())
            res = json.loads(r["response"].read().decode())
        except Exception as e:
            if time.time() - t0 > 90 and not first: return None, int(time.time() - t0), str(e)[:160]
            time.sleep(5); continue
        reach = frozenset(x["service"] for x in res["results"] if x["call"].startswith(("OK", "REACHED")))
        for sv in reach: first.setdefault(sv, int(time.time() - t0))
        same = same + 1 if reach == last else 1
        if reach != last: last_change = int(time.time() - t0)
        last = reach
        if same >= 3: break
        time.sleep(15)
    return res, last_change, first


def time_to_reach(rid, service, tag, max_wait=900):
    """After a deploy has COMPLETED, poll a warm session every 10s until `service` is reachable. Returns seconds, or None."""
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{boto3.client('sts').get_caller_identity()['Account']}:runtime/{rid}"
    sid = f"{tag}-{uuid.uuid4().hex}-session"; t0 = time.time(); probes = 0
    while time.time() - t0 < max_wait:
        try:
            r = rt.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sid, payload=json.dumps({"input": {"services": [service], "include_global_sts": False}}).encode())
            x = json.loads(r["response"].read().decode())["results"][0]; probes += 1
            if x["call"].startswith(("OK", "REACHED")): return int(time.time() - t0), probes, x
        except Exception:
            pass
        time.sleep(10)
    return None, probes, None


def lag():
    """Measure how long after CloudFormation reports an endpoint deployed the service is actually reachable from the runtime."""
    full = STEPS[-1][2]; O = outputs(); rid = find_runtime()
    for victim, svc in (("xray", "xray"), ("sts", "sts"), ("monitoring", "monitoring")):
        without = [e for e in full if e != victim]
        say(f"\n== lag test: {victim} =="); deploy(True, without)
        r, probes, x = time_to_reach(rid, svc, "gone", max_wait=120)
        say(f"  with the {victim} endpoint removed: {'STILL reachable' if r is not None else 'unreachable'} ({probes} probes)")
        say(f"  re-adding {victim}"); deploy(True, full)
        t, probes, x = time_to_reach(rid, svc, "back")
        say(f"  {svc} became reachable {t}s after the stack deploy finished ({probes} probes)" if t is not None else f"  {svc} NOT reachable within 900s")


def log_state(rid):
    g = f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT"
    try:
        st = logs.describe_log_streams(logGroupName=g)["logStreams"]; return f"runtime log group exists, {len(st)} streams"
    except logs.exceptions.ResourceNotFoundException: return "runtime log group does NOT exist"


def run_all():
    O = outputs(); results = {}
    say("== reset to S0 (baseline) =="); deploy(*STEPS[0][1:])
    O = outputs(); rid = ensure_runtime(O)
    for label, s3gw, eps in STEPS:
        say(f"\n== {label} =="); deploy(s3gw, eps)
        states = wait_endpoints(O)
        res, secs_stable, first = stable_probe(rid, label.split()[0].lower())
        say(f"  runtime log state: {log_state(rid)}")
        if res is None:
            say(f"  INVOKE FAILED (no probe ever answered) after {secs_stable}s: {first}"); results[label] = ("runtime did not start", []); continue
        say(f"  results stable {secs_stable}s after the endpoints reported available; first reachable at (s): {json.dumps(first)}")
        rows = []
        for x in res["results"]:
            ok = x["call"].startswith(("OK", "REACHED"))
            rows.append((x["service"], ok, x["call"], x.get("dns_private"), x.get("tcp")))
            say(f"    {x['service']:32} private_dns={str(x.get('dns_private')):5} tcp={x.get('tcp'):>12}  {x['call']}")
        results[label] = ("started", rows)
    say("\n== summary: services reachable after each step ==")
    for label, (state, rows) in results.items():
        reach = [r[0] for r in rows if r[1]]
        say(f"  {label}: {state}" + (f" | reachable: {', '.join(reach) if reach else 'none'}" if rows else ""))


def destroy():
    t0 = time.time()
    rid = find_runtime()
    if rid:
        ctl.delete_agent_runtime(agentRuntimeId=rid); say(f"deleting runtime {rid}")
        while True:
            try: ctl.get_agent_runtime(agentRuntimeId=rid); time.sleep(10)
            except ctl.exceptions.ResourceNotFoundException: break
        say(f"  runtime gone after {int(time.time()-t0)}s")
    t1 = time.time()
    for i in range(1, 50):
        r = cdk(["destroy", "--force"])
        if r.returncode == 0: say(f"stack destroyed on attempt {i}, {int(time.time()-t1)}s after the runtime was deleted"); return
        enis = ec2.describe_network_interfaces(Filters=[{"Name": "interface-type", "Values": ["agentic_ai"]}])["NetworkInterfaces"]
        say(f"  destroy attempt {i} failed ({int(time.time()-t1)}s); {len(enis)} AgentCore ENIs still present; waiting 10 minutes")
        time.sleep(600)
    say("gave up waiting; re-run destroy later")


if __name__ == "__main__":
    {"all": run_all, "destroy": destroy, "lag": lag}.get(sys.argv[1] if len(sys.argv) > 1 else "all", run_all)()
