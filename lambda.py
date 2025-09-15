"""
AWS Lambda Function for Managing Default VPC Resources

This script contains functions to manage AWS VPC resources, including retrieving
a list of available regions and deleting internet gateways (IGWs) associated with
a given VPC. It uses the boto3 library to interact with AWS services.

Usage:
    This script is intended to be used as part of an AWS Lambda function. It requires
    appropriate IAM permissions to interact with AWS EC2 resources.

Author:
    Comm-IT 2024
    Andrey Voroshnin
"""

from __future__ import annotations
import os
import boto3
import logging
import cfnresponse  # type: ignore
from botocore.exceptions import ClientError
from typing import TYPE_CHECKING, Any
from concurrent.futures import ThreadPoolExecutor


if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client, EC2ServiceResource

# Configure logging
logger = logging.getLogger()
logging.basicConfig(format="%(asctime)s %(message)s")
logger.setLevel(logging.getLevelName(os.getenv("logger_level", "INFO")))


def get_regions() -> list[str]:
    """
    Retrieves a list of all available AWS regions.

    This function uses the boto3 library to create an EC2 client and calls the
    describe_regions method to get information about all available AWS regions.
    It then extracts the region names and returns them as a list of strings.

    Returns:
        list[str]: A list of region names as strings.
    """
    ec2_client: EC2Client = boto3.client("ec2", region_name=os.getenv("AWS_REGION"))

    region_list = []
    regions = ec2_client.describe_regions()
    for region in regions["Regions"]:
        region_list.append(region["RegionName"])
    return region_list


def get_vpcs_to_delete(
    ec2_client: EC2Client, delete_default_vpcs: bool, delete_ct_vpcs: bool
) -> list[str]:
    """
    Retrieve a list of VPC IDs to delete based on specified criteria.

    Args:
        ec2_client (EC2Client): An EC2 client instance to interact with AWS EC2 service.
        delete_default_vpcs (bool): Whether to delete default VPCs.
        delete_ct_vpcs (bool): Whether to delete Control Tower managed VPCs.

    Returns:
        list[str]: A list of VPC IDs to delete.
    """
    vpc_list = []

    try:
        if delete_default_vpcs:
            try:
                default_vpcs = ec2_client.describe_vpcs(
                    Filters=[
                        {
                            "Name": "isDefault",
                            "Values": ["true"],
                        },
                    ]
                )

                for vpc in default_vpcs.get("Vpcs", []):
                    vpc_list.append(vpc["VpcId"])
                    logger.info(f"Found default VPC to delete: {vpc['VpcId']}")
            except ClientError as e:
                logger.error(f"Error retrieving default VPCs: {str(e)}")
                # Continue to next filter rather than raising exception

        if delete_ct_vpcs:
            try:
                ct_vpcs = ec2_client.describe_vpcs(
                    Filters=[
                        {
                            "Name": "tag:Name",
                            "Values": ["aws-controltower-VPC"],
                        },
                    ]
                )

                for vpc in ct_vpcs.get("Vpcs", []):
                    if vpc["VpcId"] not in vpc_list:  # Avoid duplicates
                        vpc_list.append(vpc["VpcId"])
                        logger.info(
                            f"Found Control Tower VPC to delete: {vpc['VpcId']}"
                        )
            except ClientError as e:
                logger.error(f"Error retrieving Control Tower VPCs: {str(e)}")
                # Continue rather than raising exception

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        error_msg = str(e)
        if "UnauthorizedOperation" in error_code:
            logger.warning(
                f"Access denied in region {ec2_client.meta.region_name}: {error_msg}. Skipping region."
            )
        else:
            logger.error(f"Error retrieving VPCs: {error_msg}")
            raise

    return vpc_list


def delete_igw(ec2_resource: EC2ServiceResource, vpcid: str) -> None:
    """
    Detach and delete the internet gateway associated with a given VPC.

    Parameters:
        ec2_resource (EC2ServiceResource): The EC2 resource object to interact with AWS EC2 service.
        vpcid (str): The ID of the VPC whose internet gateway needs to be deleted.

    Raises:
        ClientError: If there is an error detaching or deleting the internet gateway.
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)
    igws = vpc_resource.internet_gateways.all()
    if igws:
        for igw in igws:
            try:
                logger.info(f"Detaching and Removing igw-id: {igw.id}")
                igw.detach_from_vpc(VpcId=vpcid)
                igw.delete()
            except ClientError as e:
                logger.error(f"Failed to delete IGW: {e}")
                raise


def delete_sub(ec2_resource: EC2ServiceResource, vpcid: str) -> None:
    """
    Deletes the default subnets within a specified VPC.

    This function identifies and deletes all default subnets within the given VPC.
    It logs the ID of each subnet being deleted and handles any client errors that occur
    during the deletion process.

    Args:
        ec2_resource (EC2ServiceResource): The EC2 resource object used to interact with AWS.
        vpcid (str): The ID of the VPC from which default subnets will be deleted.

    Raises:
        ClientError: If there is an error deleting any of the subnets.

    Returns:
        None
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)
    subnets = vpc_resource.subnets.all()
    default_subnets = [
        ec2_resource.Subnet(subnet.id) for subnet in subnets if subnet.default_for_az
    ]

    if default_subnets:
        for sub in default_subnets:
            try:
                logger.info(f"Removing sub-id: {sub.id}")
                sub.delete()
            except ClientError as e:
                logger.error(f"Error deleting subnet {sub.id}: {e}")
                raise


def delete_rtb(ec2_resource: EC2ServiceResource, vpcid: str) -> None:
    """
    Deletes all non-main route tables associated with a given VPC.

    This function retrieves all route tables associated with the specified VPC
    and deletes those that are not marked as the main route table.

    Args:
        ec2_resource (EC2ServiceResource): The EC2 resource object to interact with AWS EC2.
        vpcid (str): The ID of the VPC whose route tables are to be deleted.

    Raises:
        ClientError: If there is an error deleting a route table.

    Returns:
        None
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)
    rtbs = vpc_resource.route_tables.all()
    if rtbs:
        for rtb in rtbs:
            # Logic error: This gets associations for all route tables repeatedly
            assoc_attr = [rtb.associations_attribute for rtb in rtbs]
            # Logic error: This will always use the first route table's associations
            if [
                rtb_ass[0]["RouteTableId"]
                for rtb_ass in assoc_attr
                if rtb_ass[0]["Main"] == True
            ]:
                logger.info(f"{rtb.id} is the main route table, skipping deletion...")
                continue
            try:
                logger.info(f"Removing rtb-id: {rtb.id}")
                table = ec2_resource.RouteTable(id=rtb.id)
                table.delete()
            except ClientError as e:
                logger.error(f"Error deleting route table {rtb.id}: {e}")
                raise


def delete_acl(ec2_resource: EC2ServiceResource, vpcid: str) -> None:
    """
    Deletes non-default network ACLs associated with a given VPC.

    This function retrieves all network ACLs associated with the specified VPC
    and deletes those that are not the default ACL. Default ACLs are skipped.

    Args:
        ec2_resource (EC2ServiceResource): A Boto3 EC2 resource instance.
        vpcid (str): The ID of the VPC whose non-default ACLs are to be deleted.

    Raises:
        ClientError: If there is an error deleting a network ACL.

    Returns:
        None
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)
    acls = vpc_resource.network_acls.all()

    if acls:
        for acl in acls:
            if acl.is_default:
                logger.info(f"{acl.id} is the default NACL, skipping deletion...")
                continue
            try:
                logger.info(f"Removing acl-id: {acl.id}")
                acl.delete()
            except ClientError as e:
                logger.error(f"Error deleting NACL {acl.id}: {e}")
                raise


def delete_sgr(ec2_resource: EC2ServiceResource, vpcid: str) -> None:
    """
    Deletes all security groups in a specified VPC except for the default security group.

    Args:
        ec2_resource (EC2ServiceResource): The EC2 resource object to interact with AWS EC2.
        vpcid (str): The ID of the VPC from which to delete the security groups.

    Raises:
        ClientError: If there is an error deleting a security group.

    Returns:
        None
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)
    sgrs = vpc_resource.security_groups.all()
    if sgrs:
        for sgr in sgrs:
            if sgr.group_name == "default":
                logger.info(
                    f"{sgr.id} is the default security group, skipping deletion..."
                )
                continue
            try:
                logger.info(f"Removing sgr-id: {sgr.id}")
                sgr.delete()
            except ClientError as e:
                logger.error(f"Error deleting security group {sgr.id}: {e}")
                raise


def delete_vpc(ec2_resource: EC2ServiceResource, vpcid: str) -> None:
    """
    Deletes a specified VPC using the provided EC2 resource.

    Args:
        ec2_resource (EC2ServiceResource): The EC2 resource object to interact with AWS.
        vpcid (str): The ID of the VPC to be deleted.

    Raises:
        ClientError: If there is an error deleting the VPC, such as dependencies that need to be removed first.

    Returns:
        None
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)
    try:
        logger.info(f"Removing vpc-id: {vpc_resource.id}")
        vpc_resource.delete()
    except ClientError as e:
        logger.error(f"Error deleting VPC {vpc_resource.id}: {e}")
        logger.error("Please remove dependencies and delete VPC manually.")
        raise


def delete_resources_in_vpc(ec2_resource: EC2ServiceResource, vpc: str) -> None:
    """
    Deletes various resources associated with a given VPC.

    This function sequentially deletes the following resources in the specified VPC:
    - Internet Gateways (IGWs)
    - Subnets
    - Route Tables (RTBs)
    - Network ACLs
    - Security Groups (SGRs)
    - The VPC itself

    Args:
        ec2_resource (boto3.resources.factory.ec2.ServiceResource): The EC2 resource object.
        vpc (boto3.resources.factory.ec2.Vpc): The VPC object to delete resources from.

    Returns:
        None
    """
    delete_igw(ec2_resource, vpc)
    delete_sub(ec2_resource, vpc)
    delete_rtb(ec2_resource, vpc)
    delete_acl(ec2_resource, vpc)
    delete_sgr(ec2_resource, vpc)
    delete_vpc(ec2_resource, vpc)


def delete_resources_in_region(
    region: str, delete_default_vpcs: bool, delete_ct_vpcs: bool
) -> None:
    """
    Deletes resources in the specified AWS region based on VPC criteria.

    This function initializes the EC2 client and resource for the given region,
    retrieves the VPCs to delete based on criteria, and deletes the resources within
    each VPC.

    Args:
        region (str): The AWS region where the resources should be deleted.
        delete_default_vpcs (bool): Whether to delete default VPCs.
        delete_ct_vpcs (bool): Whether to delete Control Tower managed VPCs.

    Returns:
        None
    """
    ec2_client = boto3.client("ec2", region_name=region)
    ec2_resource = boto3.resource("ec2", region_name=region)

    # Use the new function that handles both VPC types
    vpcs = get_vpcs_to_delete(ec2_client, delete_default_vpcs, delete_ct_vpcs)

    if vpcs:
        logger.info(f"Found {len(vpcs)} VPCs to delete in region {region}")
        for vpc in vpcs:
            delete_resources_in_vpc(ec2_resource, vpc)
    else:
        logger.info(f"No VPCs to delete in region {region}")


def lambda_handler(event: dict[str, Any], context: Any) -> None:
    cf_action = event["RequestType"].upper()

    # Get parameters and convert string values to boolean
    cf_delete_default_vpc = (
        event.get("ResourceProperties", {}).get("DeleteDefaultVPCs", "true").lower()
        == "true"
    )
    cf_delete_ct_vpc = (
        event.get("ResourceProperties", {})
        .get("DeleteControlTowerVPCs", "false")
        .lower()
        == "true"
    )

    logger.info(
        f"Parameters: DeleteDefaultVPCs={cf_delete_default_vpc}, DeleteControlTowerVPCs={cf_delete_ct_vpc}"
    )

    if cf_action == "CREATE":
        # Check if any VPC type is selected for deletion
        if not cf_delete_default_vpc and not cf_delete_ct_vpc:
            logger.info("No VPC types selected for deletion. Skipping operation.")
            cfnresponse.send(
                event,
                context,
                cfnresponse.SUCCESS,
                {"Message": "No VPC types selected for deletion"},
            )
            return

        regions = get_regions()
        logger.info(f"Found regions: {regions}")
        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [
                    executor.submit(
                        delete_resources_in_region,
                        region,
                        cf_delete_default_vpc,
                        cf_delete_ct_vpc,
                    )
                    for region in regions
                ]
                # Should collect errors rather than stopping at first failure
                errors = []
                for region, future in zip(regions, futures):
                    try:
                        future.result()
                    except Exception as e:
                        errors.append(f"Error in region {region}: {str(e)}")
                        logger.error(f"Error processing region {region}: {e}")

                if errors:
                    error_message = "; ".join(errors)
                    cfnresponse.send(
                        event, context, cfnresponse.FAILED, {"Error": error_message}
                    )
                    return
        except ClientError as e:
            logger.error(f"Unexpected Error: {e}")
            errorText = e.response["Error"]["Message"]
            logger.error(f"Error Text: {errorText}")
            cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": errorText})
            return
        except Exception as e:
            logger.error(f"Unhandled exception: {e}")
            cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": str(e)})
            return

        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            {
                "Regions": regions,
                "DeletedDefaultVPCs": str(cf_delete_default_vpc),
                "DeletedControlTowerVPCs": str(cf_delete_ct_vpc),
            },
        )
    else:
        logger.info(f"Skipping {cf_action} operation...")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
