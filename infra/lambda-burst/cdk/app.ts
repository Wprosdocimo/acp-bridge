#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { LambdaBurstStack } from "./stack";

const app = new cdk.App();

new LambdaBurstStack(app, "AcpBridgeLambdaBurst", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
  description: "ACP Bridge Lambda Burst - Serverless harness-factory agents",
});
