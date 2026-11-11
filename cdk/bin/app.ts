import * as cdk from 'aws-cdk-lib';
import { ProbeStack } from '../lib/stack';

const app = new cdk.App();
new ProbeStack(app, 'PrivateSubnetProbe', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.DEMO_REGION ?? 'us-east-1' },
});
