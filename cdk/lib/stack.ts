import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';

// VPC with NO NAT gateway and NO internet gateway route. Which endpoints exist is driven by CDK context so the demo can add them step by step:
//   -c endpoints=sts,logs,...   interface endpoints to create (service names as in com.amazonaws.<region>.<name>)
//   -c s3gw=true|false          S3 gateway endpoint
// The AgentCore Runtime itself is created through the API by demo/demo.py, because CloudFormation cannot set requireServiceS3Endpoint.
export class ProbeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    const endpoints = String(this.node.tryGetContext('endpoints') ?? '').split(',').map(s => s.trim()).filter(Boolean);
    const s3gw = String(this.node.tryGetContext('s3gw') ?? 'false') === 'true';
    const region = this.region, account = this.account;

    // us-east-1a and us-east-1b are use1-az2 and use1-az4 in the demo account; AgentCore VPC mode supports use1-az1, az2 and az4 only.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      availabilityZones: [`${region}a`, `${region}b`],
      natGateways: 0,
      subnetConfiguration: [{ name: 'private', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 }],
      restrictDefaultSecurityGroup: false,
    });
    const runtimeSg = new ec2.SecurityGroup(this, 'RuntimeSg', { vpc, description: 'AgentCore runtime ENIs', allowAllOutbound: true });
    const endpointSg = new ec2.SecurityGroup(this, 'EndpointSg', { vpc, description: 'Interface endpoints', allowAllOutbound: false });
    endpointSg.addIngressRule(runtimeSg, ec2.Port.tcp(443), 'HTTPS from the runtime');

    if (s3gw) vpc.addGatewayEndpoint('S3Gateway', { service: ec2.GatewayVpcEndpointAwsService.S3 });
    for (const spec of endpoints) {
      const [name, flag] = spec.split(':');   // "kafka:nopd" = no private DNS (the kafka service needs acceptance, which blocks private DNS)
      new ec2.InterfaceVpcEndpoint(this, `Ep-${name.replace(/[^A-Za-z0-9]/g, '-')}`, {
        vpc, service: new ec2.InterfaceVpcEndpointService(`com.amazonaws.${region}.${name}`, 443),
        subnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED }, securityGroups: [endpointSg], privateDnsEnabled: flag !== 'nopd',
      });
    }

    const bucket = new s3.Bucket(this, 'Code', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY, autoDeleteObjects: true,
    });

    // A role the probe can assume, so the STS AssumeRole call is a real one. Same account: cross-account is not tested.
    const execRole = new iam.Role(this, 'ExecRole', { assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com') });
    const targetRole = new iam.Role(this, 'TargetRole', { assumedBy: new iam.ArnPrincipal(execRole.roleArn) });
    const target = (actions: string[], resources: string[] = ['*']) => execRole.addToPolicy(new iam.PolicyStatement({ actions, resources }));
    bucket.grantRead(execRole);                      // the runtime reads its code package from here
    target(['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'], ['arn:aws:bedrock:*::foundation-model/*', `arn:aws:bedrock:${region}:${account}:*`]);
    target(['logs:CreateLogGroup', 'logs:DescribeLogStreams', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogGroups']);
    target(['xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:GetSamplingRules', 'xray:GetSamplingTargets']);
    target(['cloudwatch:PutMetricData', 'cloudwatch:ListMetrics']);
    target(['sts:AssumeRole'], [targetRole.roleArn]);
    // Read-only calls for the services the asker names. Granted so that any failure is the network, not IAM.
    target(['ecr:DescribeRepositories', 'ce:GetCostAndUsage', 'ses:GetAccount', 'rds:DescribeDBInstances',
            'elasticloadbalancing:DescribeLoadBalancers', 'kafka:ListClustersV2']);
    target(['bedrock-agentcore:GetWorkloadAccessToken', 'bedrock-agentcore:GetWorkloadAccessTokenForJWT']);

    new cdk.CfnOutput(this, 'Region', { value: region });
    new cdk.CfnOutput(this, 'VpcId', { value: vpc.vpcId });
    new cdk.CfnOutput(this, 'SubnetIds', { value: vpc.isolatedSubnets.map(s => s.subnetId).join(',') });
    new cdk.CfnOutput(this, 'RuntimeSgId', { value: runtimeSg.securityGroupId });
    new cdk.CfnOutput(this, 'ExecRoleArn', { value: execRole.roleArn });
    new cdk.CfnOutput(this, 'TargetRoleArn', { value: targetRole.roleArn });
    new cdk.CfnOutput(this, 'CodeBucket', { value: bucket.bucketName });
  }
}
