# Copyright (c) 2019 SUSE LINUX GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import logging
from typing import List
import threading
import time
import string
import random

import boto3

from tests.config import settings
from tests.lib.hardware.hardware_base import HardwareBase
from tests.lib.hardware.node_base import NodeBase, NodeRole
from tests.lib.workspace import Workspace


logger = logging.getLogger(__name__)


class Node(NodeBase):
    def __init__(self, name: str, role: NodeRole, tags: List[str],
                 ec2: boto3.resources.base.ServiceResource,
                 subnet: boto3.resources.base.ServiceResource,
                 security_group: boto3.resources.base.ServiceResource,
                 keypair: boto3.resources.base.ServiceResource):
        super().__init__(name, role, tags)
        self._name = name
        self._role = role
        self._tags = tags
        self._ec2 = ec2
        self._subnet = subnet
        self._security_group = security_group
        self._keypair = keypair
        self._instance = None

    def boot(self):
        instances = self._ec2.create_instances(
            ImageId=settings.AWS.AMI_IMAGE_ID,
            InstanceType=settings.AWS.NODE_SIZE,
            MinCount=1,
            MaxCount=1,
            SecurityGroupIds=[
                self._security_group.id,
            ],
            KeyName=self._keypair.name,
            SubnetId=self._subnet.id,
        )

        self._instance = instances[0]
        self._instance.create_tags(
            Tags=[{"Key": "Name", "Value": self._name}])
        self._instance.wait_until_exists()

        logger.info(f"Created Node {self._instance}")

        if self._role == NodeRole.WORKER:
            for i in range(0, settings.WORKER_INITIAL_DATA_DISKS):
                disk_name = self.disk_create(10)
                self.disk_attach(name=disk_name)

    def get_ssh_ip(self) -> str:
        if not self._instance:
            return ""
        # The IP address may not be ready immediately. If that's the case,
        # try reloading the instance information a reasonable number of times.
        attempts = 60
        while self._instance.public_ip_address is None:
            time.sleep(3)
            self._instance.reload()
            attempts -= 1
            if attempts <= 0:
                raise Exception(
                    f"Unable to get public IP for instance {self._instance}")
        return self._instance.public_ip_address

    def _get_vol_name_by_vol(self, volume):
        for k, v in self._disks.items():
            if v['volume'].id == volume.id:
                return k

    def disk_create(self, capacity):
        super().disk_create(capacity)
        if not self._instance:
            raise Exception("Can not create a disk until a node is created")
        suffix = ''.join(random.choice(string.ascii_lowercase)
                         for i in range(5))
        name = f"{self._name}-volume-{suffix}"
        volume = self._ec2.create_volume(
            AvailabilityZone=self._instance.placement['AvailabilityZone'],
            Size=capacity,
        )
        volume.create_tags(
            Tags=[{"Key": "Name", "Value": name}])
        self._disks[name] = {'volume': volume, 'attached': False}
        logger.info(f"disk {name} ({volume}) created")
        return name

    def _get_next_device_name(self):
        if not self._instance:
            return None
        self._instance.reload()
        all_device_names = set(
            ["/dev/xvd%s" % (x) for x in string.ascii_lowercase])
        used_device_names = set()
        for device in self._instance.block_device_mappings:
            used_device_names.add(device['DeviceName'])

        return list(all_device_names - used_device_names).pop()

    def disk_attach(self, name=None, volume=None):
        if name is None and volume is None:
            raise Exception("Please specify either name or volume parameter")
        if name is not None:
            volume = self._disks[name]['volume']
        else:
            name = self._get_vol_name_by_vol(volume)

        if not self._instance:
            raise Exception("Unable to attach a disk before booting instance")
        self._instance.wait_until_running()

        # wait until volume is available
        attempts = 0
        while volume.state != "available":
            if attempts >= 10:
                raise Exception(f"Volume {volume} did not become available")
            time.sleep(5)
            volume.reload()
            attempts += 1

        volume.attach_to_instance(
            Device=self._get_next_device_name(),
            InstanceId=self._instance.id,
        )

        self._disks[name]['attached'] = True
        logger.info(f"Volume {name} attached to {self._instance}")
        self._instance.reload()

    def disk_detach(self, name):
        volume = self._disks[name]['volume']
        volume.detach_from_instance()
        self._disks[name]['attached'] = False
        logger.info(f"Volume {name} detached")
        if self._instance:
            self._instance.reload()

    def destroy(self):
        super().destroy()
        if self._instance:
            self._instance.terminate()
            self._instance.wait_until_terminated()
            for k, v in self._disks.items():
                _id = v['volume'].id
                v['volume'].delete()
                logger.info(f"Deleted volume {k} ({_id})")


class Hardware(HardwareBase):
    def __init__(self, workspace: Workspace):
        super().__init__(workspace)
        self._workspace = workspace
        self._ec2 = self.get_connection()

        # basic setup needed for all nodes
        self._vpc = self._create_vpc()
        self._gateway = self._create_gateway()
        self._routetable = self._create_routetable()
        self._subnet = self._create_subnet()
        self._security_group = self._create_security_group()
        self._keypair = self._import_keypair()

    def get_connection(self):
        return boto3.resource('ec2')

    def _create_vpc(self):
        vpc = self._ec2.create_vpc(
            CidrBlock='192.168.100.0/24'
        )
        vpc.create_tags(
            Tags=[{"Key": "Name", "Value":  f"{self.workspace.name}-vpc"}]
        )
        vpc.wait_until_available()
        logger.info(f"Created VPC {vpc}")
        return vpc

    def _create_gateway(self):
        # Create gateway and attach to VPC
        gateway = self._ec2.create_internet_gateway()
        gateway.create_tags(
            Tags=[{"Key": "Name", "Value": f"{self.workspace.name}-gateway"}]
        )
        self._vpc.attach_internet_gateway(InternetGatewayId=gateway.id)
        logger.info(f"Created Gateway {gateway} (attached to VPC {self._vpc})")
        return gateway

    def _create_routetable(self):
        # create a route table for VPC and a public route
        routetable = self._vpc.create_route_table()
        routetable.create_tags(
            Tags=[{
                "Key": "Name",
                "Value": f"{self.workspace.name}-routetable"
            }]
        )
        route = routetable.create_route(
            DestinationCidrBlock='0.0.0.0/0', GatewayId=self._gateway.id)
        logger.info(
            f"Created routetable {routetable} (inside VPC {self._vpc})"
            f" with route {route}"
        )
        return routetable

    def _create_subnet(self):
        # Create subnet in VPC
        subnet = self._vpc.create_subnet(
            CidrBlock='192.168.100.0/25'
        )
        subnet.create_tags(
            Tags=[{"Key": "Name", "Value": f"{self.workspace.name}-subnet"}]
        )
        subnet.meta.client.modify_subnet_attribute(
            SubnetId=subnet.id, MapPublicIpOnLaunch={"Value": True}
        )
        logger.info(f"Created subnet {subnet} (inside VPC {self._vpc})")
        self._routetable.associate_with_subnet(SubnetId=subnet.id)
        logger.info(
            f"Associated routetable {self._routetable} with subnet {subnet}")
        return subnet

    def _create_security_group(self):
        # Create security group
        security_group = self._vpc.create_security_group(
            GroupName=f"{self.workspace.name}-sg",
            Description='Permissive security group for rookcheck',
        )
        security_group.create_tags(
            Tags=[{"Key": "Name", "Value": f"{self.workspace.name}-sg"}])
        security_group.authorize_ingress(
            CidrIp='0.0.0.0/0',
            IpProtocol='-1',
            FromPort=0,
            ToPort=65535,
        )
        logger.info(
            f"Created security group {security_group}"
            f" (inside VPC {self._vpc})"
        )
        return security_group

    def _import_keypair(self):
        keypair = self._ec2.import_key_pair(
            KeyName=f"{self.workspace.name}-key",
            PublicKeyMaterial=self.workspace.public_key
        )

        logger.info(f"Created keypair {keypair}")
        return keypair

    def node_create(self, name: str, role: NodeRole,
                    tags: List[str]) -> NodeBase:
        super().node_create(name, role, tags)
        node = Node(
            name, role, tags,
            self._ec2, self._subnet, self._security_group, self._keypair,
        )
        node.boot()
        return node

    def _node_create_add(self, name: str, role: NodeRole,
                         tags: List[str]):
        node = self.node_create(name, role, tags)
        self.node_add(node)

    def boot_nodes(self, masters: int, workers: int, offset: int = 0):
        super().boot_nodes(masters, workers, offset)
        threads = []
        for m in range(0, masters):
            if m == 0:
                tags = ['master', 'first_master']
            else:
                tags = ['master']
            node_name = "%s-master-%d" % (self.workspace.name, m+offset)

            thread = threading.Thread(
                target=self._node_create_add, args=(node_name, NodeRole.MASTER,
                                                    tags))
            threads.append(thread)
            thread.start()

        for m in range(0, workers):
            tags = ['worker']
            node_name = "%s-worker-%d" % (self.workspace.name, m+offset)
            thread = threading.Thread(
                target=self._node_create_add, args=(node_name, NodeRole.WORKER,
                                                    tags))
            threads.append(thread)
            thread.start()

        # wait for all threads to finish
        for t in threads:
            t.join()

    def destroy(self, skip=False):
        super().destroy(skip=skip)

        if skip:
            if self._keypair:
                logger.warning(f"Leaving keypair {self._keypair}")
            if self._security_group:
                logger.warning(
                    f"Leaving security group {self._security_group}")
            if self._subnet:
                logger.warning(f"Leaving subnet {self._subnet}")
            if self._routetable:
                logger.warning(f"Leaving routetable {self._routetable}")
            if self._gateway:
                logger.warning(f"Leaving gateway {self._gateway}")
            if self._vpc:
                logger.warning(f"Leaving VPC {self._vpc}")
            return

        self._keypair.delete()
        logger.info(f"Deleted keypair {self._keypair}")
        self._security_group.delete()
        logger.info(f"Deleted security group {self._security_group}")
        self._subnet.delete()
        logger.info(f"Deleted subnet {self._subnet}")
        self._routetable.delete()
        logger.info(f"Deleted routetable {self._routetable}")
        self._vpc.detach_internet_gateway(InternetGatewayId=self._gateway.id)
        logger.info(f"Detached gateway {self._gateway} from VPC {self._vpc}")
        self._gateway.delete()
        logger.info(f"Deleted gateway {self._gateway}")
        self._vpc.delete()
        logger.info(f"Deleted vpc {self._vpc}")
