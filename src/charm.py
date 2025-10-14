#!/usr/bin/env python3
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

import base64
import configparser
import logging
import os
import re
import socket
import subprocess
import sys
import traceback
from typing import Any, Mapping

from charms.operator_libs_linux.v0 import apt
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

logger = logging.getLogger(__name__)

APT_CONF_OVERRIDE = "/etc/apt/apt.conf.d/99landscapeoverride"
CERT_FILE = "/etc/ssl/certs/landscape_server_ca.crt"
CLIENT_CONF_FILE = "/etc/landscape/client.conf"
CLIENT_CONFIG_CMD = "/usr/bin/landscape-config"
CLIENT_PACKAGE = "landscape-client"

CHARM_ONLY_CONFIGS = {
    "ppa",
    "disable-unattended-upgrades",
    "additional-client-configuration",
}
"""
Configuration values that are only meaningful for the charm and should not be passed
through to Landscape client.
"""


class ClientCharmError(Exception):
    pass


def log_error(text, event=None):
    if text:  # Sometimes the subprocess output is empty
        logger.critical(text)
    if event:
        event.log(text)
        event.fail()


def log_info(text, event=None):
    text = "LANDSCAPE CLIENT CHARM INFO: {}".format(text)
    if text:  # Sometimes the subprocess output is empty
        logger.info(text)
    if event:
        event.log(text)


def write_certificate(certificate, filename):
    """
    @param certificate Text of the certificate, base64 encoded.
    @param filename Full path to file to write
    """
    with open(filename, "wb") as file:
        file.write(base64.b64decode(certificate))


def parse_ssl_arg(value):
    """
    Check for file, return if exists.
    Otherwise decode from base64 and write to_location
    """
    if os.path.isfile(value):
        return value
    try:
        write_certificate(value, CERT_FILE)
        value = CERT_FILE
    except OSError:
        log_error("Cert {} does not exist!".format(value))
        raise ClientCharmError("Certificate does not exist!")

    return value


def get_modified_env_vars():
    """
    Because the python path gets munged by the juju env, this grabs the current
    env vars and returns a copy with the juju env removed from the python paths
    """
    env_vars = os.environ.copy()
    logging.info("Fixing python paths")
    new_paths = [path for path in sys.path if "juju" not in path]
    env_vars["PYTHONPATH"] = ":".join(new_paths)
    return env_vars


def process_helper(args, hide_errors=False, env=get_modified_env_vars()):
    """
    Grabs all outputs and exceptions from subprocess and look for
    keywords that indicate failure and return if successful or not
    If hide errors flag is enabled, then suppresses output, which
    is used for commands that are expected to return non-zero
    """
    log_info(args)
    try:
        p = subprocess.Popen(
            args, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, text=True, env=env
        )
    except Exception:
        log_error(traceback.format_exc())
        return False
    output, error = p.communicate()
    if p.returncode != 0 or "Failure" in output:
        if not hide_errors:
            log_error("".join(traceback.format_stack()))
            log_error(output)
            log_error(error)
        return False
    else:
        log_info(output)
        return True


def merge_client_config(conf_file: str, client_config: Mapping[str, Any]):
    """
    Read the contents of the [client] section in `conf_file` and merge `client_config`,
    overwriting existing values.
    """
    config = configparser.ConfigParser()
    config.read(conf_file)

    config["client"].update({k: str(v) for k, v in client_config.items() if v})

    with open(conf_file, "w") as configfile:
        config.write(configfile)

    c = {s: dict(config[s]) for s in config.sections()}
    logger.info(f"Client configuration merged. Current value: {c}")


def get_additional_client_configuration(
    juju_config: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Parse the `additional-client-configuration` option from the Juju configuration
    and return key-value pairs.

    Return an empty dictionary if the `additional-client-configuration` option is not
    provided or cannot be parsed.
    """
    raw = juju_config.get("additional-client-configuration")
    logger.debug(f"Received additional-client-configuration: {repr(raw)}")
    if not raw:
        return {}

    config = configparser.ConfigParser()

    try:
        config.read_string(raw)
    except configparser.MissingSectionHeaderError as e:
        raise ClientCharmError(
            f"Malformed additional-client-configuration: {repr(raw)}"
        ) from e

    try:
        client_config = dict(config["client"])
        logger.debug(f"Parsed additional-client-configuration: {client_config}")
        return client_config
    except KeyError as e:
        raise ClientCharmError(
            f"Malformed additional-client-configuration: {repr(raw)}"
        ) from e


def create_client_config(
    juju_config: Mapping[str, Any],
    default_computer_title: str,
) -> dict:
    """
    Create the Landscape client configuration from the Juju configuration.

    Remove any Juju configuration that is not relevant to client, and set
    default values if applicable.

    Juju config values with multiple words are hyphen-separated, but client
    values are underscore-separated.
    """
    client_config = {
        key.replace("-", "_"): value
        for key, value in juju_config.items()
        if key not in CHARM_ONLY_CONFIGS
    }

    additional_configuration = get_additional_client_configuration(juju_config)
    client_config.update(additional_configuration)

    client_config.setdefault("computer_title", default_computer_title)

    if ssl_key := client_config.get("ssl_ca"):
        client_config["ssl_ca"] = parse_ssl_arg(ssl_key)
    elif ssl_key := client_config.get("ssl_public_key"):
        client_config["ssl_public_key"] = parse_ssl_arg(ssl_key)
        logging.warning("`ssl_public_key` is deprecated; " "use `ssl_ca` instead.")
    return client_config


class LandscapeClientCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(
            self.on.container_relation_departed, self._on_relation_departed
        )
        self.framework.observe(self.on.upgrade_action, self._upgrade)
        self.framework.observe(self.on.register_action, self._register)
        self._stored.set_default(things=[])

    def add_ppa(self):
        landscape_ppa = self.config.get("ppa")
        if landscape_ppa:
            self.unit.status = MaintenanceStatus("Adding client PPA..")

            # add-apt-repository doesn't use the proxy configuration from apt
            # or juju. If we find any juju_proxy setting or application config,
            # add the classic http(s)_proxy to the env. Only necessary for this
            # specific call
            add_apt_repository_env = os.environ.copy()
            for proxy_var in ["http_proxy", "https_proxy"]:
                juju_proxy_var = f"JUJU_CHARM_{proxy_var.upper()}"

                # if the charm has a proxy setting configured, override the
                # juju_http(s)_proxy configuration from the model
                if proxy_var.replace("_", "-") in self.config:
                    add_apt_repository_env[proxy_var] = self.config[
                        proxy_var.replace("_", "-")
                    ]
                elif juju_proxy_var in add_apt_repository_env:
                    add_apt_repository_env[proxy_var] = add_apt_repository_env[
                        juju_proxy_var
                    ]

                if proxy_var in add_apt_repository_env:
                    logger.info(
                        f"add-apt-repository {proxy_var} variable set to : "
                        f"{add_apt_repository_env[proxy_var]}"
                    )

            if not process_helper(
                ["add-apt-repository", "-y", landscape_ppa], env=add_apt_repository_env
            ):
                raise ClientCharmError("Failed to add PPA!")

    def install_landscape_client(self):
        self.unit.status = MaintenanceStatus("Installing landscape client..")
        try:
            apt.add_package(CLIENT_PACKAGE)
        except Exception:
            log_error(traceback.format_exc())
            raise ClientCharmError("Failed to install client!")

    def set_client_config(self):
        """
        Gets and processes the landscape client config args
        from the charm configuration
        """
        client_config = create_client_config(
            juju_config=self.config,
            default_computer_title=socket.gethostname(),
        )
        log_info(client_config)
        merge_client_config(CLIENT_CONF_FILE, client_config)

    def is_registered(self):
        return process_helper([CLIENT_CONFIG_CMD, "--is-registered"], hide_errors=True)

    def send_registration(self):
        if process_helper([CLIENT_CONFIG_CMD, "--silent"]):
            self.unit.status = ActiveStatus("Client registered!")
        else:
            raise ClientCharmError("Registration failed!")

    def run_landscape_client(self):
        self.unit.status = MaintenanceStatus("Configuring landscape client..")
        self.set_client_config()
        if self.is_registered():
            process_helper(["systemctl", "restart", "landscape-client"])
            self.unit.status = ActiveStatus("Client config updated!")
        else:
            self.send_registration()

    def _on_install(self, _):
        try:
            self.add_ppa()
            self.install_landscape_client()
        except ClientCharmError as exc:
            self.unit.status = BlockedStatus(str(exc))

    def _on_config_changed(self, _):
        if self.config.get("disable-unattended-upgrades"):
            log_info("Disabling unattended-upgrades via APT config...")
            with open(APT_CONF_OVERRIDE, "w") as override_fp:
                override_fp.write('APT::Periodic::Unattended-Upgrade "0";')
        elif os.path.exists(APT_CONF_OVERRIDE):
            log_info("Enabling unattended-upgrades via APT config...")
            os.remove(APT_CONF_OVERRIDE)

        try:
            apt.DebianPackage.from_installed_package(CLIENT_PACKAGE)
        except apt.PackageNotFoundError:
            log_error("Landscape client package not installed.")
            return
        try:
            self.add_ppa()
            self.run_landscape_client()
        except ClientCharmError as exc:
            self.unit.status = BlockedStatus(str(exc))

    def _on_relation_departed(self, _):
        """Disable landscape client when relation is broken"""
        self.unit.status = MaintenanceStatus("Disabling landscape client..")
        process_helper([CLIENT_CONFIG_CMD, "--silent", "--disable"])

    def _upgrade(self, event):
        if isinstance(self.unit.status, MaintenanceStatus):
            log_error("Please wait until charm is ready before upgrading.", event=event)
            return

        self.add_ppa()
        apt.update()

        try:
            log_info("Upgrading landscape client..", event=event)
            pkg = apt.DebianPackage.from_apt_cache(CLIENT_PACKAGE)
            pkg.ensure(state=apt.PackageState.Latest)
            installed = apt.DebianPackage.from_installed_package(CLIENT_PACKAGE)
            log_info("Upgraded to {}...".format(installed.version), event=event)
        except Exception as exc:
            log_error("Could not upgrade landscape client!", event=event)
            log_error(traceback.format_exc(), event=event)
            self.unit.status = BlockedStatus(str(exc))

    def _register(self, event):
        if isinstance(self.unit.status, MaintenanceStatus):
            log_error(
                "Please wait until charm is ready before registering.", event=event
            )
            return

        try:
            log_info("Registering landscape client..", event=event)
            self.send_registration()
            log_info("Registration successful!", event=event)
        except Exception as exc:
            log_error("Could not register landscape client!", event=event)
            log_error(traceback.format_exc(), event=event)
            self.unit.status = BlockedStatus(str(exc))


if __name__ == "__main__":
    main(LandscapeClientCharm)
