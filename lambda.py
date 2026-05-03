from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import cfnresponse  # type: ignore
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logging.basicConfig(format="%(asctime)s %(message)s")
logger.setLevel(logging.getLevelName(os.getenv("logger_level", "INFO")))


def get_regions() -> list[str]:
    """
    Retrieves a list of all available AWS regions or just the current region.
    """
    current_region = os.getenv("AWS_REGION")
    if os.getenv("DELETE_IN_ALL_REGIONS", "true").lower() != "true":
        logger.info(f"Processing only current region: {current_region}")
        return [current_region] if current_region else []

    ec2_client = boto3.client("ec2", region_name=current_region)

    region_list = []
    regions = ec2_client.describe_regions()
    for region in regions["Regions"]:
        region_list.append(region["RegionName"])
    return region_list


def get_vpcs_to_delete(
    ec2_client, delete_default_vpcs: bool, delete_ct_vpcs: bool
) -> list[str]:
    """
    Retrieve a list of VPC IDs to delete based on specified criteria.
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


def delete_natgw(ec2_resource, vpcid: str) -> None:
    """
    Deletes all NAT Gateways in the given VPC.
    """
    ec2_client = ec2_resource.meta.client

    try:
        nat_gws = ec2_client.describe_nat_gateways(
            Filter=[{"Name": "vpc-id", "Values": [vpcid]}]
        ).get("NatGateways", [])

        nat_ids = [ng["NatGatewayId"] for ng in nat_gws if "NatGatewayId" in ng]
        if not nat_ids:
            logger.info(f"No NAT gateways found in VPC {vpcid}")
            return

        logger.info(f"Deleting NAT gateways in VPC {vpcid}: {nat_ids}")
        for nat_id in nat_ids:
            ec2_client.delete_nat_gateway(NatGatewayId=nat_id)

        waiter = ec2_client.get_waiter("nat_gateway_deleted")
        try:
            waiter.wait(NatGatewayIds=nat_ids)
            logger.info(f"All NAT gateways deleted in VPC {vpcid}")
        except Exception as e:
            logger.error(f"Error waiting for NAT gateway deletion in VPC {vpcid}: {e}")
            raise RuntimeError(
                f"Failed waiting for NAT gateway deletion in VPC {vpcid}: {e}"
            )

    except ClientError as e:
        logger.error(f"Error deleting NAT gateways in VPC {vpcid}: {e}")
        raise


def delete_igw(ec2_resource, vpcid: str) -> None:
    """
    Detach and delete the internet gateway associated with a given VPC.
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


def delete_sub(ec2_resource, vpcid: str) -> None:
    """
    Deletes subnets within a specified VPC.

    - For default VPCs: deletes only AWS default subnets (default_for_az == True)
    - For non-default VPCs (e.g. Control Tower): deletes all subnets
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)
    vpc_resource.load()

    subnets = list(vpc_resource.subnets.all())

    if vpc_resource.is_default:
        subnets_to_delete = [s for s in subnets if s.default_for_az]
    else:
        subnets_to_delete = subnets

    if not subnets_to_delete:
        logger.info(f"No subnets to delete in VPC {vpcid}")
        return

    for subnet in subnets_to_delete:
        try:
            logger.info(f"Deleting subnet: {subnet.id}")
            subnet.delete()
        except ClientError as e:
            logger.error(f"Error deleting subnet {subnet.id}: {e}")
            raise


def delete_rtb(ec2_resource, vpcid: str) -> None:
    """
    Deletes all non-main route tables associated with a given VPC.
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)

    for rtb in vpc_resource.route_tables.all():
        is_main = any(assoc.get("Main", False) for assoc in rtb.associations_attribute)

        if is_main:
            logger.info(f"{rtb.id} is the main route table, skipping deletion")
            continue

        try:
            logger.info(f"Deleting route table: {rtb.id}")
            rtb.delete()
        except ClientError as e:
            logger.error(f"Error deleting route table {rtb.id}: {e}")
            raise


def delete_acl(ec2_resource, vpcid: str) -> None:
    """
    Deletes non-default network ACLs associated with a given VPC.
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


def delete_sgr(ec2_resource, vpcid: str) -> None:
    """
    Deletes all security groups in a specified VPC except for the default security group.
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


def delete_vpc(ec2_resource, vpcid: str) -> None:
    """
    Deletes a specified VPC using the provided EC2 resource.
    """
    vpc_resource = ec2_resource.Vpc(id=vpcid)
    try:
        logger.info(f"Removing vpc-id: {vpc_resource.id}")
        vpc_resource.delete()
    except ClientError as e:
        logger.error(f"Error deleting VPC {vpc_resource.id}: {e}")
        logger.error("Please remove dependencies and delete VPC manually.")
        raise


def delete_resources_in_vpc(ec2_resource, vpc: str) -> None:
    """
    Deletes various resources associated with a given VPC.

    This function sequentially deletes the following resources in the specified VPC:
    - Internet Gateways (IGWs)
    - Subnets
    - Route Tables (RTBs)
    - Network ACLs
    - Security Groups (SGRs)
    - The VPC itself
    """
    delete_natgw(ec2_resource, vpc)
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
