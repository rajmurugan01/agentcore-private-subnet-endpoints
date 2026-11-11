# Which VPC endpoints does an AgentCore Runtime agent need with no NAT gateway?

A runnable test for [this re:Post question](https://repost.aws/questions/QU3fz6TmsCS2KfzJdtPJMdVA): an agent on
Amazon Bedrock AgentCore Runtime in private subnets with no NAT, and a request for the full list of VPC endpoints
"up front instead of finding gaps one timeout at a time". The existing answer on that thread gives the documented list (ECR, an S3 gateway
endpoint, CloudWatch Logs and the two AgentCore endpoints) and says a Regional STS endpoint is enough. This repo checks that on a running
agent. It is a measured addition to that answer, not a replacement.

A **probe agent** runs inside the runtime and, in one invocation, tells you for each AWS service whether it can reach it: DNS result,
TCP connect, and a real API call with short timeouts. Adding endpoints step by step shows which ones matter.

## Setup tested

One runtime, direct code deploy (a Python zip, not a container image), in two private subnets in us-east-1 with **no NAT gateway and no
internet gateway route**. Same-account IAM role. The probe calls: STS (including a real `AssumeRole`), CloudWatch Logs, CloudWatch metrics,
S3, Bedrock runtime (a 5-token Converse call), X-Ray, ECR, Cost Explorer, SES v2, RDS, ELB and MSK APIs, plus the **global** STS
endpoint `sts.amazonaws.com`. The IAM role is granted read-only access to all of them, so a failure is the network, not IAM.

## Results

| Question | Result (measured) |
|---|---|
| Runtime with no endpoints at all | Creates and reaches READY (about 4.5 minutes), but **invocation fails**. Two forms seen: `Runtime initialization time exceeded. Please make sure that initialization completes in 30s.` and `Received error (502) from runtime`. Neither message mentions networking |
| What the runtime needs to start (direct code deploy) | **Only an S3 gateway endpoint.** With just that, the runtime started and the probe ran. Every other service, including the global STS endpoint, was unreachable |
| `requireServiceS3Endpoint` | Cannot be set when creating a runtime (`requireServiceS3Endpoint cannot be set during agent creation.`) and cannot be modified on agents created after 2026-06-11 (`Agents created after 2026-06-11T00:00:00Z cannot modify requireServiceS3Endpoint.`). My runtime reported `false`. For a new runtime, your own S3 gateway endpoint is the route. CloudFormation and CDK do not expose this field at all |
| What the agent's own calls need | One interface endpoint per service. With the S3 gateway endpoint plus interface endpoints for `logs`, `sts`, `monitoring`, `bedrock-runtime`, `xray`, `ecr.api`, `ce`, `email`, `rds` and `elasticloadbalancing`, **12 of the 14 probes reached their service** (STS `GetCallerIdentity` and `AssumeRole`, Logs, metrics, S3, Bedrock, X-Ray, ECR, Cost Explorer, SES, RDS, ELB). The two that did not were MSK (below) and the global STS endpoint |
| Necessity, shown directly | For `xray`, `sts` and `monitoring`: with the endpoint removed the service was unreachable; with it re-added the service was reachable. One run each |
| Regional vs global STS | The Regional endpoint was enough for STS calls and `AssumeRole` (same account). `sts.amazonaws.com` timed out in every run. Cross-account was not tested |
| Endpoint availability in us-east-1 | All the asker's services have one: `sts`, `logs`, `monitoring`, `bedrock-runtime`, `xray`, `ecr.api`, `ecr.dkr`, `ce`, `email` and `email-smtp` (SES; there is no endpoint named `ses`), `rds`, `elasticloadbalancing`, `kafka`, `bedrock-agentcore`, `bedrock-agentcore-control`. Check your own Region with `aws ec2 describe-vpc-endpoint-services` |

## Three things that would have cost a day

**1. An endpoint is not usable when CloudFormation says it is.** Every endpoint reported `available` the moment the stack finished, and
the service was still unreachable. After re-adding an endpoint, the service became reachable **155 to 179 seconds later** (X-Ray 161 s,
STS 155 s, CloudWatch metrics 179 s; one run each). My first run probed immediately after each step and so credited each step with the
endpoints of the one before. That transcript is kept in
[docs/transcript-first-run-probed-too-early.txt](docs/transcript-first-run-probed-too-early.txt) as evidence of the effect. If your agent
still times out right after you add an endpoint, wait a few minutes before deciding it is the wrong one.

**2. `kafka` is an acceptance-required endpoint service.** Creating the MSK API endpoint with private DNS failed:
`Private DNS can only be enabled after the endpoint connection is accepted by the owner of com.amazonaws.us-east-1.kafka.`
`describe-vpc-endpoint-services` shows `AcceptanceRequired: True`. Created without private DNS, the endpoint sat in `pendingAcceptance`, and the
MSK API stayed unreachable. I did not find out whether or how it gets accepted, and I did not test connecting to MSK brokers, which is a different path.

**3. The first invoke is close to the limit.** With only the S3 endpoint, the first invocation of a new session took about 29 seconds against
the 30-second initialisation limit. A cold start in a VPC has little headroom.

## Teardown

AgentCore creates service-managed network interfaces (interface type `agentic_ai`, one per subnet) in your subnets, and they outlive the runtime. I deleted the runtime and it was gone in 44 seconds, but `cdk destroy` then failed on both subnets and the runtime security group ("has dependencies" / "has a dependent object") on every retry. At the time of writing, more than 4 hours after the runtime was deleted, the two interfaces were still attached and the stack was still in `DELETE_FAILED`. The documentation says they can persist for up to 8 hours; I have not yet measured the actual release time and will update this section when I have. The interface endpoints were deleted first and the VPC, subnets and security group have no charge while waiting. Plan for this if you automate teardown: retry, do not treat the first failure as a bug. `make destroy` retries every 10 minutes.

## What I did not test

- Container-image deploys (the documented `ecr.dkr` and `ecr.api` endpoints matter there; I did not test `ecr.dkr`), the Browser tool (which needs internet through a NAT gateway), and Regions other than us-east-1.
- Cross-account `AssumeRole`, centralised versus per-account endpoint design (that needs several accounts), and VPC endpoint policies.
- A workaround for a service with no interface endpoint in your Region. Every service the asker names has one in us-east-1, so this did not come up. Candidate patterns would be a proxy or a function outside the VPC.
- MSK data-plane (broker) connectivity, and the lag figures beyond one run per service.

## Run it

Needs an AWS account with CDK bootstrapped in us-east-1, Node 20+, Python 3.10+ with boto3 and pip.

    make deploy      # baseline stack: VPC with no NAT, roles, bucket
    make demo        # steps S0 to S5, 60 to 90 minutes
    make lag         # the endpoint-lag measurement
    make destroy     # deletes the runtime and the stack; may wait hours for network interfaces

The runtime is created through the API by the demo script, because CloudFormation cannot set `requireServiceS3Endpoint`. The step transcript
([docs/transcript-steps.txt](docs/transcript-steps.txt)) shows results that lag the endpoints they follow, which is the effect described above,
so use the Results table, not that file's per-step "reachable" lists, for what each service needs.

## Cost

List price is $0.01 per VPC endpoint hour in us-east-1 (AWS Price List API, usage type `USE1-VpcEndpoint-Hours`). I did not verify how that
applies per Availability Zone. The 11 interface endpoints lived about an hour in total across two AZs, so on that arithmetic at most a few tens
of cents, plus data processing at $0.01 per GB, which a probe barely uses. I did not price the runtime or the Bedrock calls. This is an estimate
from list prices, not a measured bill.
