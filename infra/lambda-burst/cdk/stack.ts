import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import * as path from "path";

/**
 * ACP Bridge Lambda Burst Stack
 *
 * Deploys harness-factory as a Lambda function for serverless agent scaling.
 * Sensitive values (API keys) are stored in Secrets Manager and read at runtime,
 * never baked into environment variables or CloudFormation templates.
 */
export class LambdaBurstStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ========== Context parameters ==========
    // Non-sensitive: can come from context or env
    const vpcId = this.requireContext("vpcId", "VPC_ID");
    const subnetIds = this.requireContext("subnetIds", "SUBNET_IDS")
      .split(",")
      .filter(Boolean);
    const securityGroupId =
      this.node.tryGetContext("securityGroupId") ||
      process.env.SECURITY_GROUP_ID ||
      "";
    const litellmUrl =
      this.node.tryGetContext("litellmUrl") ||
      process.env.LITELLM_URL ||
      "";
    const defaultModel =
      this.node.tryGetContext("defaultModel") ||
      process.env.DEFAULT_MODEL ||
      "bedrock/anthropic.claude-sonnet-4-6";
    const maxConcurrency = parseInt(
      this.node.tryGetContext("maxConcurrency") ||
        process.env.MAX_CONCURRENCY ||
        "100",
      10
    );
    // Optional: provide an existing Secrets Manager ARN for the LiteLLM API key.
    // If empty, CDK creates a new secret (you must populate it post-deploy).
    const existingSecretArn =
      this.node.tryGetContext("litellmSecretArn") ||
      process.env.LITELLM_SECRET_ARN ||
      "";

    // ========== Validation ==========
    if (!vpcId) {
      throw new Error(
        "vpcId is required. Pass via --context vpcId=vpc-xxx or VPC_ID env var."
      );
    }
    if (subnetIds.length === 0) {
      throw new Error(
        "subnetIds is required. Pass comma-separated subnet IDs."
      );
    }
    if (!litellmUrl) {
      throw new Error(
        "litellmUrl is required (private VPC address of your LiteLLM, e.g. http://10.0.1.79:4000)."
      );
    }

    // ========== VPC lookup ==========
    const vpc = ec2.Vpc.fromLookup(this, "Vpc", { vpcId });

    const subnets: ec2.ISubnet[] = subnetIds.map((sid, i) =>
      ec2.Subnet.fromSubnetId(this, `Subnet${i}`, sid)
    );

    const securityGroup = securityGroupId
      ? ec2.SecurityGroup.fromSecurityGroupId(
          this,
          "LambdaSG",
          securityGroupId
        )
      : new ec2.SecurityGroup(this, "LambdaSG", {
          vpc,
          description: "ACP Bridge Lambda Burst - egress to LiteLLM only",
          allowAllOutbound: false,
        });

    // If we created the SG, restrict egress to LiteLLM port only
    if (!securityGroupId) {
      const litellmPort = parseInt(new URL(litellmUrl).port || "4000", 10);
      (securityGroup as ec2.SecurityGroup).addEgressRule(
        ec2.Peer.ipv4(vpc.vpcCidrBlock),
        ec2.Port.tcp(litellmPort),
        "Allow outbound to LiteLLM within VPC"
      );
      // HTTPS for Bedrock API (if model calls go direct)
      (securityGroup as ec2.SecurityGroup).addEgressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(443),
        "Allow HTTPS for Bedrock API"
      );
    }

    // ========== Secrets Manager (API key) ==========
    let secret: secretsmanager.ISecret;
    if (existingSecretArn) {
      // Use existing secret
      secret = secretsmanager.Secret.fromSecretCompleteArn(
        this,
        "LitellmSecret",
        existingSecretArn
      );
    } else {
      // Create a new secret placeholder — user must populate after deploy
      secret = new secretsmanager.Secret(this, "LitellmSecret", {
        secretName: "/acp-bridge/lambda-burst/litellm-api-key",
        description:
          "LiteLLM API key for Lambda Burst agents. Populate after deploy: aws secretsmanager put-secret-value --secret-id /acp-bridge/lambda-burst/litellm-api-key --secret-string sk-your-key",
        generateSecretString: {
          // Placeholder — user replaces with real key
          generateStringKey: "apiKey",
          secretStringTemplate: JSON.stringify({ apiKey: "REPLACE_ME" }),
        },
      });
    }

    // ========== Lambda Layer (harness-factory binary) ==========
    // Layer structure: layer/bin/harness-factory (executable)
    const harnessLayer = new lambda.LayerVersion(this, "HarnessFactoryLayer", {
      layerVersionName: "harness-factory",
      description: "harness-factory Go binary (static, ~6MB)",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "layer")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      compatibleArchitectures: [lambda.Architecture.X86_64],
    });

    // ========== Lambda Function ==========
    const fn = new lambda.Function(this, "HarnessBurstFn", {
      functionName: "acp-bridge-harness-burst",
      description: "ACP Bridge - serverless harness-factory agent executor",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "wrapper")),
      layers: [harnessLayer],
      memorySize: 512,
      timeout: cdk.Duration.minutes(5),
      ephemeralStorageSize: cdk.Size.mebibytes(1024),
      vpc,
      vpcSubnets: { subnets },
      securityGroups: [securityGroup as ec2.ISecurityGroup],
      environment: {
        // Non-sensitive config only
        HARNESS_BIN: "/opt/bin/harness-factory",
        LITELLM_URL: litellmUrl,
        DEFAULT_MODEL: defaultModel,
        AGENT_TIMEOUT: "300",
        // Reference to secret ARN — handler reads at runtime
        LITELLM_SECRET_ARN: secret.secretArn,
      },
      reservedConcurrentExecutions: maxConcurrency,
    });

    // Grant Lambda read access to the secret
    secret.grantRead(fn);

    // Bedrock access (if harness-factory calls Bedrock directly for some models)
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: ["*"],
      })
    );

    // ========== Outputs ==========
    new cdk.CfnOutput(this, "FunctionName", {
      value: fn.functionName,
      description: "Lambda function name — use in Bridge config.yaml lambda_pool.function_name",
    });

    new cdk.CfnOutput(this, "FunctionArn", {
      value: fn.functionArn,
    });

    new cdk.CfnOutput(this, "LayerArn", {
      value: harnessLayer.layerVersionArn,
    });

    new cdk.CfnOutput(this, "SecretArn", {
      value: secret.secretArn,
      description:
        "Secrets Manager ARN. Populate with: aws secretsmanager put-secret-value --secret-id <arn> --secret-string <your-api-key>",
    });

    new cdk.CfnOutput(this, "BridgeConfigSnippet", {
      value: [
        "# Add to config.yaml:",
        "lambda_pool:",
        "  enabled: true",
        `  function_name: ${fn.functionName}`,
        `  region: ${this.region}`,
        `  max_concurrent: ${maxConcurrency}`,
        "  timeout: 300",
      ].join("\n"),
      description: "Config snippet for Bridge config.yaml",
    });
  }

  /** Require a context value, falling back to env var. */
  private requireContext(contextKey: string, envKey: string): string {
    return (
      this.node.tryGetContext(contextKey) || process.env[envKey] || ""
    );
  }
}
