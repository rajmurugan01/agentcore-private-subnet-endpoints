# Region is pinned: a shell's AWS_REGION can silently override AWS_DEFAULT_REGION for CLI calls.
export AWS_REGION ?= us-east-1
export AWS_DEFAULT_REGION ?= us-east-1

install:
	cd cdk && npm install
	python3 -m pip install --quiet boto3

# Builds the direct-code-deploy zip (main.py plus boto3, Linux arm64).
package:
	./scripts/build_package.sh

# Baseline stack only: VPC with no NAT and no endpoints, roles, bucket. The demo adds endpoints itself.
deploy: install package
	cd cdk && eval "$$(aws configure export-credentials --format env)" && npx cdk deploy --require-approval never --outputs-file cdk.outputs.json -c endpoints= -c s3gw=false

# Steps S0 to S5 (adds endpoints each step). About 60 to 90 minutes, mostly waiting for endpoints to become usable.
demo:
	python3 demo/demo.py all | tee docs/transcript.txt

# Time how long an endpoint takes to become usable AFTER CloudFormation says it is deployed.
lag:
	python3 demo/demo.py lag | tee docs/transcript-lag.txt

# Deletes the runtime, then the stack. AgentCore network interfaces can keep subnets and security groups busy for hours; this retries.
destroy:
	python3 demo/demo.py destroy | tee docs/transcript-destroy.txt

.PHONY: install package deploy demo lag destroy
