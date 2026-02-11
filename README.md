# AWS Clean Default VPC

## Description

This project provides an AWS CloudFormation template and a Lambda function to delete default and optionally Control Tower-managed VPCs across all AWS regions in an account. It is designed to help maintain a clean and organized AWS environment.

## Features

- Deletes default VPCs in all AWS regions.
- Optionally deletes Control Tower-managed VPCs (use with caution).
- Fully automated using AWS CloudFormation and Lambda.

## Files

- **clean-default-vpc.yaml**: AWS CloudFormation template to deploy the solution.
- **lambda.py**: Python script for the Lambda function that performs the VPC deletion.
- **requirements.txt**: Python dependencies for the Lambda function.

## Prerequisites

- AWS CLI configured with appropriate permissions.
- Python environment to install dependencies.
- IAM permissions to manage VPCs, Lambda, and CloudFormation resources.

## Deployment

### Step 1: Install Dependencies

Ensure you have Python installed. Install the required dependencies using pip:

```bash
pip install -r requirements.txt
```

### Step 2: Deploy the CloudFormation Stack

Use the AWS CLI to deploy the CloudFormation stack:

```bash
aws cloudformation deploy \
  --template-file clean-default-vpc.yaml \
  --stack-name CleanDefaultVPC \
  --capabilities CAPABILITY_NAMED_IAM
```

### Step 3: Parameters

The CloudFormation template accepts the following parameters:

- **pDeleteDefaultVPCs**: Set to `true` to delete default VPCs (default: `true`).
- **pDeleteControlTowerVPCs**: Set to `true` to delete Control Tower-managed VPCs (default: `false`).

### Step 4: Verify

Check the CloudFormation stack events and Lambda logs to verify the deletion process.

## Lambda Function

The Lambda function performs the following tasks:

1. Retrieves a list of all AWS regions.
2. Identifies VPCs to delete based on the parameters.
3. Deletes the identified VPCs using multi-threading for efficiency.

## License

This project is licensed under the MIT License. See the LICENSE file for details.

## Disclaimer

Use this tool with caution, especially when enabling the deletion of Control Tower-managed VPCs. Ensure you understand the impact of deleting VPCs in your AWS account.
