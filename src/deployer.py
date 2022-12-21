import re
from typing import Dict

from racetrack_client.client.env import merge_env_vars
from racetrack_client.log.logs import get_logger
from racetrack_client.manifest import Manifest
from racetrack_client.utils.shell import CommandError, shell, shell_output
from racetrack_client.utils.time import datetime_to_timestamp, now
from racetrack_commons.api.tracing import get_tracing_header_name
from racetrack_commons.deploy.image import get_fatman_image
from racetrack_commons.deploy.resource import fatman_resource_name
from racetrack_commons.entities.dto import FatmanDto, FatmanStatus, FatmanFamilyDto
from racetrack_commons.plugin.core import PluginCore
from racetrack_commons.plugin.engine import PluginEngine
from lifecycle.auth.subject import get_auth_subject_by_fatman_family
from lifecycle.config import Config
from lifecycle.deployer.base import FatmanDeployer
from lifecycle.deployer.secrets import FatmanSecrets
from lifecycle.fatman.models_registry import read_fatman_family_model

from plugin_config import InfrastructureConfig

FATMAN_INTERNAL_PORT = 7000  # Fatman listening port seen from inside the container

logger = get_logger(__name__)


class DockerDaemonDeployer(FatmanDeployer):
    """FatmanDeployer managing workloads on a remote docker instance"""
    def __init__(self, infrastructure_target: str, infra_config: InfrastructureConfig, docker_config_dir: str) -> None:
        super().__init__()
        self.infra_config = infra_config
        self.infrastructure_target = infrastructure_target
        self.docker_config_dir = docker_config_dir

    def deploy_fatman(
        self,
        manifest: Manifest,
        config: Config,
        plugin_engine: PluginEngine,
        tag: str,
        runtime_env_vars: Dict[str, str],
        family: FatmanFamilyDto,
        containers_num: int = 1,
    ) -> FatmanDto:
        """Run Fatman as docker container on local docker"""
        if self.fatman_exists(manifest.name, manifest.version):
            self.delete_fatman(manifest.name, manifest.version)

        fatman_port = self._get_next_fatman_port()
        entrypoint_resource_name = fatman_resource_name(manifest.name, manifest.version)
        deployment_timestamp = datetime_to_timestamp(now())
        family_model = read_fatman_family_model(family.name)
        auth_subject = get_auth_subject_by_fatman_family(family_model)

        assert self.infra_config.hostname, 'hostname of a docker daemon must be set'
        internal_name = f'{self.infra_config.hostname}:{fatman_port}'

        common_env_vars = {
            'PUB_URL': config.internal_pub_url,
            'FATMAN_NAME': manifest.name,
            'AUTH_TOKEN': auth_subject.token,
            'FATMAN_DEPLOYMENT_TIMESTAMP': deployment_timestamp,
            'REQUEST_TRACING_HEADER': get_tracing_header_name(),
        }
        if config.open_telemetry_enabled:
            common_env_vars['OPENTELEMETRY_ENDPOINT'] = config.open_telemetry_endpoint

        if containers_num > 1:
            common_env_vars['FATMAN_USER_MODULE_HOSTNAME'] = self.get_container_name(entrypoint_resource_name, 1)

        conflicts = common_env_vars.keys() & runtime_env_vars.keys()
        if conflicts:
            raise RuntimeError(f'found illegal runtime env vars, which conflict with reserved names: {conflicts}')
        runtime_env_vars = merge_env_vars(runtime_env_vars, common_env_vars)
        plugin_vars_list = plugin_engine.invoke_plugin_hook(PluginCore.fatman_runtime_env_vars)
        for plugin_vars in plugin_vars_list:
            if plugin_vars:
                runtime_env_vars = merge_env_vars(runtime_env_vars, plugin_vars)
        env_vars_cmd = ' '.join([f'--env {env_name}="{env_val}"' for env_name, env_val in runtime_env_vars.items()])

        try:
            shell(f'DOCKER_HOST={self.infra_config.docker_host} docker network create racetrack_default')
        except CommandError as e:
            if e.returncode != 1:
                raise e

        for container_index in range(containers_num):

            container_name = self.get_container_name(entrypoint_resource_name, container_index)
            image_name = get_fatman_image(config.docker_registry, config.docker_registry_namespace, manifest.name, tag, container_index)
            ports_mapping = f'-p {fatman_port}:{FATMAN_INTERNAL_PORT}' if container_index == 0 else ''

            shell(
                f' DOCKER_CONFIG={self.docker_config_dir}'
                f' DOCKER_HOST={self.infra_config.docker_host}'
                f' docker run -d'
                f' --name {container_name}'
                f' {ports_mapping}'
                f' {env_vars_cmd}'
                f' --pull always'
                f' --network="racetrack_default"'
                f' --label fatman-name={manifest.name}'
                f' --label fatman-version={manifest.version}'
                f' {image_name}'
            )

        return FatmanDto(
            name=manifest.name,
            version=manifest.version,
            status=FatmanStatus.RUNNING.value,
            create_time=deployment_timestamp,
            update_time=deployment_timestamp,
            manifest=manifest,
            internal_name=internal_name,
            image_tag=tag,
            infrastructure_target=self.infrastructure_target,
        )

    def delete_fatman(self, fatman_name: str, fatman_version: str):
        entrypoint_resource_name = fatman_resource_name(fatman_name, fatman_version)
        for container_index in range(2):
            container_name = self.get_container_name(entrypoint_resource_name, container_index)
            self._delete_container_if_exists(container_name)

    def fatman_exists(self, fatman_name: str, fatman_version: str) -> bool:
        resource_name = fatman_resource_name(fatman_name, fatman_version)
        container_name = self.get_container_name(resource_name, 0)
        return self._container_exists(container_name)

    def _container_exists(self, container_name: str) -> bool:
        output = shell_output(f'DOCKER_HOST={self.infra_config.docker_host} docker ps -a --filter "name=^/{container_name}$" --format "{{{{.Names}}}}"')
        return container_name in output.splitlines()

    def _delete_container_if_exists(self, container_name: str):
        if self._container_exists(container_name):
            shell(f'DOCKER_HOST={self.infra_config.docker_host} docker rm -f {container_name}')

    def _get_next_fatman_port(self) -> int:
        """Return next unoccupied port for Fatman"""
        output = shell_output(f'DOCKER_HOST={self.infra_config.docker_host} docker ps --filter "name=^/fatman-" --format "{{.Names}} {{.Ports}}"')
        occupied_ports = set()
        for line in output.splitlines():
            match = re.fullmatch(r'fatman-(.+) .+:(\d+)->.*', line.strip())
            if match:
                occupied_ports.add(int(match.group(2)))
        for port in range(7000, 8000, 10):
            if port not in occupied_ports:
                return port
        return 8000

    def save_fatman_secrets(
        self,
        fatman_name: str,
        fatman_version: str,
        fatman_secrets: FatmanSecrets,
    ):
        raise NotImplementedError("managing secrets is not supported on local docker")

    def get_fatman_secrets(
        self,
        fatman_name: str,
        fatman_version: str,
    ) -> FatmanSecrets:
        raise NotImplementedError("managing secrets is not supported on local docker")

    @staticmethod
    def get_container_name(resource_name: str, container_index: int) -> str:
        if container_index == 0:
            return resource_name
        else:
            return f'{resource_name}-{container_index}'
