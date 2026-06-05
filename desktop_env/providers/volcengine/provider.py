from __future__ import annotations

import atexit
import logging
import os
import time
from typing import Any

import dotenv
import requests
import volcenginesdkcore
import volcenginesdkecs.models as ecs_models
import volcenginesdkvpc.models as vpc_models
from volcenginesdkcore.rest import ApiException
from volcenginesdkecs.api import ECSApi
from volcenginesdkvpc.api import VPCApi

from desktop_env.providers.base import Provider

dotenv.load_dotenv()

logger = logging.getLogger("desktopenv.providers.volcengine.VolcengineProvider")
logger.setLevel(logging.INFO)

DEFAULT_SERVER_PORT = 5000
DEFAULT_CHROMIUM_PORT = 9222
DEFAULT_VNC_PORT = 5910
DEFAULT_VLC_PORT = 8080


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise EnvironmentError(f"{name} must be set for provider_name=volcengine")
    return value


class VolcengineProvider(Provider):
    """Volcengine ECS provider for DesktopEnv's per-task-image interface.

    It creates one ECS instance from the resolved task image, waits for the
    OSWorld server to become reachable, and returns the endpoint in the same
    five-part format used by DesktopEnv.
    """

    def __init__(self, region: str = None, *, spec: str | None = None):
        super().__init__(region or _env("VOLCENGINE_REGION", required=True))
        self.spec = spec
        self.client = self._create_client()
        self.vpc_client = self._create_vpc_client()

        self.instance_id: str | None = None
        self._image: str | None = None
        self._headless: bool = False
        self._os_type: str = ""
        self._host: str | None = None

        self.server_port = int(_env("VOLCENGINE_SERVER_PORT", str(DEFAULT_SERVER_PORT)))
        self.chromium_port = int(_env("VOLCENGINE_CHROMIUM_PORT", str(DEFAULT_CHROMIUM_PORT)))
        self.vnc_port = int(_env("VOLCENGINE_VNC_PORT", str(DEFAULT_VNC_PORT)))
        self.vlc_port = int(_env("VOLCENGINE_VLC_PORT", str(DEFAULT_VLC_PORT)))
        self.use_private_ip = _truthy(_env("VOLCENGINE_USE_PRIVATE_IP"))
        self.wait_timeout = int(_env("VOLCENGINE_OSWORLD_READY_TIMEOUT", "900"))
        self.wait_interval = int(_env("VOLCENGINE_OSWORLD_READY_INTERVAL", "10"))
        atexit.register(self.stop_emulator)

    def _create_client(self) -> ECSApi:
        configuration = volcenginesdkcore.Configuration()
        configuration.ak = _env("VOLCENGINE_ACCESS_KEY_ID", required=True)
        configuration.sk = _env("VOLCENGINE_SECRET_ACCESS_KEY", required=True)
        configuration.region = self.region
        configuration.host = _env(
            "VOLCENGINE_ECS_ENDPOINT",
            f"ecs.{self.region}.volcengineapi.com",
        )
        configuration.client_side_validation = True
        volcenginesdkcore.Configuration.set_default(configuration)
        return ECSApi()

    def _create_vpc_client(self) -> VPCApi:
        configuration = volcenginesdkcore.Configuration()
        configuration.ak = _env("VOLCENGINE_ACCESS_KEY_ID", required=True)
        configuration.sk = _env("VOLCENGINE_SECRET_ACCESS_KEY", required=True)
        configuration.region = self.region
        configuration.host = _env(
            "VOLCENGINE_VPC_ENDPOINT",
            f"vpc.{self.region}.volcengineapi.com",
        )
        configuration.client_side_validation = True
        volcenginesdkcore.Configuration.set_default(configuration)
        return VPCApi()

    def start_emulator(self, image: str, headless: bool, os_type: str) -> None:
        logger.info("Starting Volcengine ECS image=%s os_type=%s", image, os_type)
        self._image = image
        self._headless = headless
        self._os_type = os_type
        self._host = None

        try:
            self.instance_id = self._run_instance(image)
            self._wait_until_running(self.instance_id)
            self._host = self._select_host(self.instance_id)
            self._wait_osworld_server(self._host)
            self._log_access(self.instance_id)
        except Exception:
            if self.instance_id:
                try:
                    self._delete_instance(self.instance_id)
                except Exception as cleanup_error:
                    logger.warning("Cleanup after Volcengine start failure failed: %s", cleanup_error)
            self.instance_id = None
            self._host = None
            raise

    def get_ip_address(self) -> str:
        if not self.instance_id:
            raise RuntimeError("get_ip_address called before start_emulator")
        if not self._host:
            self._host = self._select_host(self.instance_id)
        return (
            f"{self._host}:{self.server_port}:{self.chromium_port}:"
            f"{self.vnc_port}:{self.vlc_port}"
        )

    def reset_to_clean(self) -> None:
        if not self._image:
            raise RuntimeError("reset_to_clean called before start_emulator")
        image = self._image
        headless = self._headless
        os_type = self._os_type
        self.stop_emulator()
        self.start_emulator(image, headless, os_type)

    def stop_emulator(self) -> None:
        instance_id = self.instance_id
        if not instance_id:
            return
        if _truthy(_env("VOLCENGINE_KEEP_INSTANCE_ON_STOP")):
            logger.warning("VOLCENGINE_KEEP_INSTANCE_ON_STOP is set; leaving %s running", instance_id)
            self.instance_id = None
            self._host = None
            return
        self._delete_instance(instance_id)
        self.instance_id = None
        self._host = None

    def _run_instance(self, image_id: str) -> str:
        instance_type = _env("VOLCENGINE_INSTANCE_TYPE")
        if not instance_type and self.spec and self.spec not in {"default", "g4il.2xlarge"}:
            instance_type = self.spec
        if not instance_type:
            raise EnvironmentError(
                "VOLCENGINE_INSTANCE_TYPE must be set for provider_name=volcengine "
                "or pass --spec with a Volcengine ECS instance type."
            )

        subnet_id = _env("VOLCENGINE_SUBNET_ID", required=True)
        security_group_id = _env("VOLCENGINE_SECURITY_GROUP_ID", required=True)
        zone_id = _env("VOLCENGINE_ZONE_ID") or self._resolve_subnet_zone(subnet_id)
        bandwidth = int(_env("VOLCENGINE_EIP_BANDWIDTH_MBPS", "5"))
        volume_size = int(_env("VOLCENGINE_VOLUME_SIZE_GB", "30"))
        volume_type = _env("VOLCENGINE_VOLUME_TYPE", "ESSD_PL0")
        name_prefix = _env("VOLCENGINE_INSTANCE_NAME_PREFIX", "osworld")

        kwargs: dict[str, Any] = {
            "image_id": image_id,
            "instance_type": instance_type,
            "network_interfaces": [
                ecs_models.NetworkInterfaceForRunInstancesInput(
                    subnet_id=subnet_id,
                    security_group_ids=[security_group_id],
                )
            ],
            "eip_address": ecs_models.EipAddressForRunInstancesInput(
                bandwidth_mbps=bandwidth,
                charge_type=_env("VOLCENGINE_EIP_CHARGE_TYPE", "PayByTraffic"),
            ),
            "instance_name": f"{name_prefix}-{os.getpid()}-{int(time.time())}",
            "volumes": [
                ecs_models.VolumeForRunInstancesInput(
                    volume_type=volume_type,
                    size=volume_size,
                )
            ],
            "zone_id": zone_id,
            "description": "OSWorld evaluation instance",
        }
        password = _env("VOLCENGINE_DEFAULT_PASSWORD")
        if password:
            kwargs["password"] = password
        key_pair_name = _env("VOLCENGINE_KEY_PAIR_NAME")
        if key_pair_name:
            kwargs["key_pair_name"] = key_pair_name

        response = self.client.run_instances(ecs_models.RunInstancesRequest(**kwargs))
        instance_ids = getattr(response, "instance_ids", None)
        if not instance_ids:
            raise RuntimeError(f"Volcengine run_instances returned no instance ids: {response!r}")
        instance_id = instance_ids[0]
        logger.info("Volcengine instance created: %s", instance_id)
        return instance_id

    def _resolve_subnet_zone(self, subnet_id: str) -> str:
        response = self.vpc_client.describe_subnets(
            vpc_models.DescribeSubnetsRequest(subnet_ids=[subnet_id])
        )
        subnets = getattr(response, "subnets", None) or []
        if not subnets:
            raise RuntimeError(f"Volcengine subnet not found: {subnet_id}")
        zone_id = getattr(subnets[0], "zone_id", None)
        if not zone_id:
            raise RuntimeError(f"Volcengine subnet {subnet_id} has no zone_id")
        logger.info("Resolved Volcengine subnet %s to zone %s", subnet_id, zone_id)
        return zone_id

    def _describe_instance(self, instance_id: str):
        response = self.client.describe_instances(
            ecs_models.DescribeInstancesRequest(instance_ids=[instance_id])
        )
        instances = getattr(response, "instances", None) or []
        if not instances:
            raise RuntimeError(f"Volcengine instance not found: {instance_id}")
        return instances[0]

    def _wait_until_running(self, instance_id: str) -> None:
        deadline = time.time() + int(_env("VOLCENGINE_INSTANCE_READY_TIMEOUT", "600"))
        while True:
            instance = self._describe_instance(instance_id)
            status = getattr(instance, "status", "")
            logger.info("Volcengine instance %s status=%s", instance_id, status)
            if status == "RUNNING":
                return
            if status in {"ERROR", "STOPPED"}:
                raise RuntimeError(f"Volcengine instance {instance_id} entered status={status}")
            if time.time() > deadline:
                raise TimeoutError(f"Timed out waiting for Volcengine instance {instance_id} to run")
            time.sleep(5)

    def _select_host(self, instance_id: str) -> str:
        instance = self._describe_instance(instance_id)
        public_ip = getattr(getattr(instance, "eip_address", None), "ip_address", None)
        private_ip = None
        network_interfaces = getattr(instance, "network_interfaces", None) or []
        if network_interfaces:
            private_ip = getattr(network_interfaces[0], "primary_ip_address", None)
        host = private_ip if self.use_private_ip else public_ip
        if not host:
            host = public_ip or private_ip
        if not host:
            raise RuntimeError(f"Volcengine instance {instance_id} has no usable IP address")
        return host

    def _wait_osworld_server(self, host: str) -> None:
        url = f"http://{host}:{self.server_port}/platform"
        deadline = time.time() + self.wait_timeout
        last_error: Exception | None = None
        while time.time() <= deadline:
            try:
                response = requests.get(url, timeout=5)
                if 200 <= response.status_code < 300:
                    logger.info("OSWorld server is ready: %s -> %s", url, response.text.strip())
                    return
                last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            except Exception as error:
                last_error = error
            time.sleep(self.wait_interval)
        raise TimeoutError(f"Timed out waiting for OSWorld server at {url}: {last_error}")

    def _log_access(self, instance_id: str) -> None:
        instance = self._describe_instance(instance_id)
        public_ip = getattr(getattr(instance, "eip_address", None), "ip_address", None)
        private_ip = None
        network_interfaces = getattr(instance, "network_interfaces", None) or []
        if network_interfaces:
            private_ip = getattr(network_interfaces[0], "primary_ip_address", None)
        if public_ip:
            logger.info("Volcengine noVNC URL: http://%s:%s/vnc.html", public_ip, self.vnc_port)
            print(f"\nVolcengine noVNC URL: http://{public_ip}:{self.vnc_port}/vnc.html")
        logger.info("Volcengine instance=%s public_ip=%s private_ip=%s", instance_id, public_ip, private_ip)

    def _delete_instance(self, instance_id: str) -> None:
        logger.info("Deleting Volcengine instance %s", instance_id)
        try:
            self.client.delete_instance(ecs_models.DeleteInstanceRequest(instance_id=instance_id))
        except ApiException as error:
            logger.error("Failed to delete Volcengine instance %s: %s", instance_id, error)
            raise
