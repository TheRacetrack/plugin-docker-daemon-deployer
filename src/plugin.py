from __future__ import annotations
from pathlib import Path
from typing import Any

from racetrack_client.log.logs import get_logger
from racetrack_client.utils.datamodel import parse_yaml_file_datamodel

logger = get_logger(__name__)

from plugin_config import PluginConfig
try:
    from lifecycle.deployer.infra_target import InfrastructureTarget
    from deployer import DockerDaemonDeployer
    from monitor import DockerDaemonMonitor
    from logs_streamer import DockerDaemonLogsStreamer
except ModuleNotFoundError:
    logger.debug('Skipping Lifecycle\'s imports')


class Plugin:

    def __init__(self):
        self.plugin_config: PluginConfig = parse_yaml_file_datamodel(self.config_path, PluginConfig)

        home_dir = Path('/home/racetrack')
        if home_dir.is_dir():

            if self.plugin_config.docker_config:
                docker_dir = home_dir / '.docker'
                docker_dir.mkdir(exist_ok=True)
                dest_config_file = docker_dir / 'config.json'
                dest_config_file.write_text(self.plugin_config.docker_config)
                dest_config_file.chmod(0o600)
                logger.info('Docker Registry config has been prepared')

            if self.plugin_config.ssh:
                ssh_dir = home_dir / '.ssh'
                ssh_dir.mkdir(exist_ok=True)
                
                for filename, content in self.plugin_config.ssh.items():
                    dest_file = ssh_dir / filename
                    dest_file.write_text(content)
                    dest_file.chmod(0o600)
                
                logger.info('SSH config has been prepared')
        
        infra_num = len(self.plugin_config.infrastracture_targets)
        logger.info(f'Docker Daemon plugin loaded with {infra_num} infrastructure targets')

    def infrastructure_targets(self) -> dict[str, Any]:
        """
        Infrastracture Targets (deployment targets) for Fatmen provided by this plugin
        :return dict of infrastructure name -> an instance of lifecycle.deployer.infra_target.InfrastructureTarget
        """
        return {
            infra_name: InfrastructureTarget(
                fatman_deployer=DockerDaemonDeployer(infra_name, infra_config),
                fatman_monitor=DockerDaemonMonitor(infra_name, infra_config),
                logs_streamer=DockerDaemonLogsStreamer(infra_name, infra_config),
            )
            for infra_name, infra_config in self.plugin_config.infrastracture_targets.items()
        }