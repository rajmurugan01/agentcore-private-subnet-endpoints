"""Connectivity probe agent for AgentCore Runtime in a VPC with no NAT.

POST /invocations  -> for each AWS service, report DNS, TCP and a real API call, in parallel, with short timeouts.
GET  /ping         -> health.
Stdlib HTTP server plus boto3 (vendored in the zip). No AgentCore SDK, so nothing here depends on another service.
"""
import json, os, socket, time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
CODE_BUCKET = os.environ.get("PROBE_BUCKET", "")
TARGET_ROLE = os.environ.get("PROBE_TARGET_ROLE_ARN", "")
CONNECT, READ = 3, 5


def host(prefix):
    return f"{prefix}.{REGION}.amazonaws.com"


def converse(c):
    return c.converse(modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                      messages=[{"role": "user", "content": [{"text": "hi"}]}], inferenceConfig={"maxTokens": 5})


def assume(c):
    return c.assume_role(RoleArn=TARGET_ROLE, RoleSessionName="vpc-probe") if TARGET_ROLE else c.get_caller_identity()


# name -> (boto3 client, hostname prefix, call)
SERVICES = {
    "sts":             ("sts", "sts", lambda c: c.get_caller_identity()),
    "sts-assume-role": ("sts", "sts", assume),
    "logs":            ("logs", "logs", lambda c: c.describe_log_groups(limit=1)),
    "monitoring":      ("cloudwatch", "monitoring", lambda c: c.list_metrics(Namespace="AgentCoreProbe")),
    "s3":              ("s3", "s3", lambda c: c.list_objects_v2(Bucket=CODE_BUCKET, MaxKeys=1)),
    "bedrock-runtime": ("bedrock-runtime", "bedrock-runtime", converse),
    "xray":            ("xray", "xray", lambda c: c.get_sampling_rules()),
    "ecr.api":         ("ecr", "api.ecr", lambda c: c.describe_repositories(maxResults=1)),
    "ce":              ("ce", "ce", lambda c: c.get_cost_and_usage(TimePeriod={"Start": "2026-11-01", "End": "2026-11-02"}, Granularity="DAILY", Metrics=["UnblendedCost"])),
    "ses":             ("sesv2", "email", lambda c: c.get_account()),
    "rds":             ("rds", "rds", lambda c: c.describe_db_instances(MaxRecords=20)),
    "elb":             ("elbv2", "elasticloadbalancing", lambda c: c.describe_load_balancers(PageSize=1)),
    "msk":             ("kafka", "kafka", lambda c: c.list_clusters_v2(MaxResults=1)),
}


def dns_tcp(h):
    out = {}
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(h, 443, socket.AF_INET)})
        out["dns"] = ips[0]; out["dns_private"] = ips[0].startswith(("10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.30.", "172.31.", "192.168."))
    except Exception as e:
        out["dns"] = f"FAIL {type(e).__name__}"; out["dns_private"] = None; out["tcp"] = "skipped"; return out
    t0 = time.time()
    try:
        socket.create_connection((out["dns"], 443), timeout=CONNECT).close(); out["tcp"] = f"ok {int((time.time()-t0)*1000)}ms"
    except Exception as e:
        out["tcp"] = f"FAIL {type(e).__name__}"
    return out


def probe(name):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
    client_name, prefix, call = SERVICES[name]
    h = host(prefix)
    res = {"service": name, "host": h, **dns_tcp(h)}
    cfg = Config(connect_timeout=CONNECT, read_timeout=READ, retries={"max_attempts": 0}, region_name=REGION)
    t0 = time.time()
    try:
        call(boto3.client(client_name, config=cfg)); res["call"] = "OK"
    except ClientError as e:
        res["call"] = f"REACHED (api answered {e.response['Error']['Code']})"
    except (ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError) as e:
        res["call"] = f"UNREACHABLE ({type(e).__name__})"
    except Exception as e:
        res["call"] = f"ERROR ({type(e).__name__}: {str(e)[:80]})"
    res["call_ms"] = int((time.time() - t0) * 1000)
    return res


def global_sts():
    import boto3
    from botocore.config import Config
    t0 = time.time()
    res = {"service": "sts-GLOBAL (sts.amazonaws.com)", "host": "sts.amazonaws.com", **dns_tcp("sts.amazonaws.com")}
    try:
        boto3.client("sts", endpoint_url="https://sts.amazonaws.com", region_name="us-east-1",
                     config=Config(connect_timeout=CONNECT, read_timeout=READ, retries={"max_attempts": 0})).get_caller_identity()
        res["call"] = "OK"
    except Exception as e:
        res["call"] = f"UNREACHABLE ({type(e).__name__})" if "imeout" in type(e).__name__ or "Connect" in type(e).__name__ else f"ERROR ({type(e).__name__})"
    res["call_ms"] = int((time.time() - t0) * 1000)
    return res


def run(payload):
    names = payload.get("services") or list(SERVICES)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(names) + 1) as ex:
        futs = [ex.submit(probe, n) for n in names if n in SERVICES]
        gfut = ex.submit(global_sts) if payload.get("include_global_sts", True) else None
        results = [f.result() for f in futs] + ([gfut.result()] if gfut else [])
    return {"region": REGION, "elapsed_s": round(time.time() - t0, 1), "results": results}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"status": "Healthy"}) if self.path.startswith("/ping") else self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception: payload = {}
        if isinstance(payload.get("input"), dict): payload = payload["input"]
        self._send(200, run(payload)) if self.path.startswith("/invocations") else self._send(404, {"error": "not found"})

    def log_message(self, *a): pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
