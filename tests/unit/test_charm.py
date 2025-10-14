# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing
import base64
import os
import tempfile
import unittest
from unittest import mock

from charms.operator_libs_linux.v0 import apt
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

import charm
from charm import (
    CLIENT_CONFIG_CMD,
    ClientCharmError,
    LandscapeClientCharm,
    create_client_config,
    get_additional_client_configuration,
    get_modified_env_vars,
)


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(LandscapeClientCharm)
        self.addCleanup(self.harness.cleanup)

        self.process_mock = mock.patch("charm.process_helper").start()
        self.apt_mock = mock.patch("charm.apt.add_package").start()
        self.from_installed_package_mock = mock.patch(
            "charm.apt.DebianPackage.from_installed_package"
        ).start()
        self.open_mock = mock.patch("builtins.open").start()
        self.open_mock.side_effect = mock.mock_open(read_data="[client]")

    def test_install(self):
        self.harness.begin_with_initial_hooks()
        self.apt_mock.assert_called_once_with("landscape-client")

    def test_install_error(self):
        self.apt_mock.side_effect = Exception
        self.from_installed_package_mock.side_effect = apt.PackageNotFoundError
        self.harness.begin_with_initial_hooks()
        self.harness.update_config()
        status = self.harness.charm.unit.status
        self.assertEqual(status.message, "Failed to install client!")
        self.assertIsInstance(status, BlockedStatus)

    @mock.patch("charm.merge_client_config")
    @mock.patch("charm.LandscapeClientCharm.is_registered", return_value=False)
    def test_run(self, is_registered_mock, merge_client_config_mock):
        """Test args get passed correctly to landscape-config and registers"""
        self.harness.begin()
        self.harness.update_config({"computer-title": "hello1"})
        self.assertIn(
            ("computer_title", "hello1"),
            merge_client_config_mock.call_args.args[1].items(),
        )
        self.process_mock.assert_called_once_with([CLIENT_CONFIG_CMD, "--silent"])
        status = self.harness.charm.unit.status
        self.assertEqual(status.message, "Client registered!")
        self.assertIsInstance(status, ActiveStatus)

    @mock.patch("charm.LandscapeClientCharm.is_registered", return_value=True)
    def test_restart_if_registered(self, is_registered_mock):
        """Restart client if it's registered"""
        self.harness.begin()
        self.harness.update_config({})
        self.process_mock.assert_called_once_with(
            ["systemctl", "restart", "landscape-client"]
        )

    def test_ppa_added(self):
        self.harness.begin()
        self.harness.update_config({"ppa": "ppa"})
        env_variables = os.environ.copy()
        self.process_mock.assert_any_call(
            ["add-apt-repository", "-y", "ppa"],
            env=env_variables,
        )

    def test_ppa_error(self):
        self.harness.begin()
        self.process_mock.return_value = False
        self.harness.update_config({"ppa": "ppa"})
        status = self.harness.charm.unit.status
        self.assertEqual(status.message, "Failed to add PPA!")
        self.assertIsInstance(status, BlockedStatus)

    @mock.patch.dict(
        os.environ,
        {
            "JUJU_CHARM_HTTP_PROXY": "http://proxy.test:3128",
            "JUJU_CHARM_HTTPS_PROXY": "http://proxy-https.test:3128",
        },
    )
    def test_ppa_added_with_proxy(self):
        self.harness.begin()
        self.harness.update_config({"ppa": "ppa"})
        env_variables = os.environ.copy()
        env_variables["http_proxy"] = "http://proxy.test:3128"
        env_variables["https_proxy"] = "http://proxy-https.test:3128"
        self.process_mock.assert_any_call(
            ["add-apt-repository", "-y", "ppa"],
            env=env_variables,
        )

    @mock.patch.dict(
        os.environ,
        {
            "JUJU_CHARM_HTTP_PROXY": "http://proxy.test:3128",
            "JUJU_CHARM_HTTPS_PROXY": "http://proxy-https.test:3128",
        },
    )
    def test_ppa_added_with_proxy_override(self):
        self.harness.begin()
        self.harness.update_config(
            {
                "ppa": "ppa",
                "http-proxy": "http://override-proxy.test:3128",
                "https-proxy": "http://override-proxy-https.test:3128",
            }
        )
        env_variables = os.environ.copy()
        env_variables["http_proxy"] = "http://override-proxy.test:3128"
        env_variables["https_proxy"] = "http://override-proxy-https.test:3128"
        self.process_mock.assert_any_call(
            ["add-apt-repository", "-y", "ppa"],
            env=env_variables,
        )

    @mock.patch("charm.merge_client_config")
    def test_ppa_not_in_args(self, merge_client_config_mock):
        """Test that the ppa arg does not end up in the landscape config"""
        self.harness.begin()
        self.harness.update_config({"ppa": "testppa"})
        self.assertNotIn("ppa", merge_client_config_mock.call_args.args[1])

    @mock.patch("charm.merge_client_config")
    def test_ssl_public_key(self, merge_client_config_mock):
        """Test that the base64 encoded ssl cert gets written successfully"""

        self.harness.begin()

        data = b"hello"
        data_b64 = base64.b64encode(data).decode()  # no 'base64:' prefix
        self.harness.update_config({"ssl-public-key": data_b64})

        self.open_mock().write.assert_called_once_with(data)

    @mock.patch("charm.merge_client_config")
    def test_ssl_cert(self, merge_client_config_mock):
        """Test that the base64 encoded ssl cert gets written successfully"""

        self.harness.begin()

        data = b"hello"
        data_b64 = base64.b64encode(data).decode()  # no 'base64:' prefix
        self.harness.update_config({"ssl-ca": data_b64})

        self.open_mock().write.assert_called_once_with(data)

    @mock.patch("charm.os.path.isfile", return_value=True)
    @mock.patch("charm.merge_client_config")
    def test_ssl_cert_valid_path(self, merge_client_config_mock, _):
        self.harness.begin()
        self.harness.update_config(
            {"ssl-ca": "/etc/ssl/certs/landscape_server_cal.crt"}
        )
        self.assertEqual(
            "/etc/ssl/certs/landscape_server_cal.crt",
            merge_client_config_mock.call_args.args[1]["ssl_ca"],
        )

    @mock.patch("charm.os.path.isfile", return_value=False)
    @mock.patch("charm.merge_client_config")
    @mock.patch("charm.write_certificate", side_effect=OSError("write failed"))
    def test_ssl_cert_oserror(
        self, _write_certificate, merge_client_config_mock, _isfile
    ):
        self.harness.begin()
        self.harness.update_config({"ssl-ca": "badcert"})
        merge_client_config_mock.assert_not_called()
        self.assertIsInstance(self.harness.model.unit.status, BlockedStatus)
        self.assertIn("Certificate does not exist", str(self.harness.model.unit.status))

    def test_relation_broken(self):
        self.harness.begin()
        rel_id = self.harness.add_relation("container", "ubuntu")
        self.harness.add_relation_unit(rel_id, "ubuntu/0")
        self.harness.remove_relation_unit(rel_id, "ubuntu/0")
        self.process_mock.assert_called_once_with(
            [CLIENT_CONFIG_CMD, "--silent", "--disable"]
        )

    def test_action_upgrade(self):
        self.harness.begin()
        self.harness.charm.unit.status = ActiveStatus("Active")
        event = mock.Mock()
        with mock.patch("charm.apt") as apt_mock:
            pkg_mock = mock.Mock()
            apt_mock.DebianPackage.from_apt_cache.return_value = pkg_mock
            self.harness.charm._upgrade(event)

        self.assertEqual(apt_mock.DebianPackage.from_apt_cache.call_count, 1)
        self.assertEqual(pkg_mock.ensure.call_count, 1)

    def test_action_register(self):
        self.harness.begin()
        self.harness.charm.unit.status = ActiveStatus("Active")
        event = mock.Mock()
        self.harness.charm._register(event)
        self.process_mock.assert_called_once_with([CLIENT_CONFIG_CMD, "--silent"])

    @mock.patch("charm.os")
    def test_disable_unattended_upgrades(self, remove_mock):
        """apt configuration is changed to disable unattended-upgrades if this
        config is `True`. If the config is changed again to `False`, the
        config override is deleted.
        """

        self.harness.begin()
        self.harness.charm.add_ppa = mock.Mock()
        self.harness.charm.run_landscape_client = mock.Mock()
        self.harness.update_config({"disable-unattended-upgrades": True})

        self.open_mock.assert_called_once_with(charm.APT_CONF_OVERRIDE, "w")

        self.harness.update_config({"disable-unattended-upgrades": False})

        remove_mock.remove.assert_called_once_with(charm.APT_CONF_OVERRIDE)

    def test_update_config(self):
        """
        Test that update config writes a new value and doesn't change previous ones
        """
        self.open_mock.side_effect = mock.mock_open(
            read_data="[client]\naccount_name = onward"
        )
        self.harness.begin()
        self.harness.update_config({"ping-url": "url"})
        text = "".join([call.args[0] for call in self.open_mock().write.mock_calls])
        self.assertIn("account_name = onward", text)
        self.assertIn("ping_url = url", text)

    @mock.patch("charm.sys.path", new=["/usr/bin", "/hello/path", "/another/path"])
    @mock.patch("charm.os.environ", new={"PYTHONPATH": "/initial/path"})
    def test_get_modified_env_vars(self):
        """
        Test that paths not having juju in them are kept the same
        """
        result = get_modified_env_vars()
        expected_paths = "/usr/bin:/hello/path:/another/path"
        self.assertEqual(result["PYTHONPATH"], expected_paths)
        self.assertNotEqual(result, os.environ)
        self.assertIn("PYTHONPATH", result)

    @mock.patch("charm.sys.path", new=["/usr/bin", "/juju/path", "/another/path"])
    @mock.patch("charm.os.environ", new={"PYTHONPATH": "/initial/path"})
    def test_juju_path_removed(self):
        result = get_modified_env_vars()
        expected_paths = "/usr/bin:/another/path"
        self.assertEqual(result["PYTHONPATH"], expected_paths)
        self.assertNotEqual(result, os.environ)
        self.assertIn("PYTHONPATH", result)

    @mock.patch("charm.merge_client_config")
    def test_additional_config(self, merge_client_config_mock):
        """
        Arbitrary configuration can be provided in a `additional_config`
        field, and it is merged into to the `client.conf`
        """
        self.harness.begin()
        self.harness.update_config(
            {
                "computer-title": "hello1",
                "additional-client-configuration": "[client]\nsomekey = someval",
            }
        )
        client_config = merge_client_config_mock.call_args.args[1].items()

        self.assertIn(("computer_title", "hello1"), client_config)
        self.assertIn(("somekey", "someval"), client_config)

    @mock.patch("charm.merge_client_config")
    def test_empty_additional_config(self, merge_client_config_mock):
        """
        Empty additional configuration has no effect.
        """
        self.harness.begin()
        self.harness.update_config(
            {
                "computer-title": "hello1",
                "additional-client-configuration": "",
            }
        )
        client_config = merge_client_config_mock.call_args.args[1].items()

        self.assertIn(("computer_title", "hello1"), client_config)


class TestCreateClientConfig(unittest.TestCase):

    def test_additional_config_merged(self):
        """
        Non-conflicting additional configuration is merged.
        """
        juju_config = {
            "account-name": "accountname",
            "additional-client-configuration": "[client]\nping_interval = 60",
        }
        expected = {
            "account_name": "accountname",
            "ping_interval": "60",
            "computer_title": "computer_title",
        }
        self.assertEqual(
            expected,
            create_client_config(juju_config, default_computer_title="computer_title"),
        )

    def test_additional_config_prioritized_over_explicit_config(self):
        """
        Keys in `additional_config` are merged on top of identical keys
        provided by explicit config.
        """
        juju_config = {
            "account-name": "accountname",
            "additional-client-configuration": "[client]\naccount_name = myadditionalaccount",
        }
        expected = {
            "account_name": "myadditionalaccount",
            "computer_title": "computer_title",
        }
        self.assertEqual(
            expected,
            create_client_config(juju_config, default_computer_title="computer_title"),
        )


class TestGetAddtionalClientConfiguration(unittest.TestCase):

    def test_empty_additional_config(self):
        """
        An empty [client] section produces an empty dict
        """
        juju_config = {"additional-client-configuration": "[client]"}
        self.assertEqual({}, get_additional_client_configuration(juju_config))

    def test_multiple_keys(self):
        """
        Multiple key/values can be specified in additional-client-configuration
        """
        juju_config = {
            "additional-client-configuration": "[client]\nsomevalue = somekey\nanother_value = another_key"
        }
        expected = {"somevalue": "somekey", "another_value": "another_key"}
        self.assertEqual(expected, get_additional_client_configuration(juju_config))

    def test_malformed_additional_config(self):
        """
        A malformed additional config raises a `ClientCharmError` and includes the
        invalid configuration in the error message.

        The `additional-client-configuration` key must start with a [client] section.
        """
        invalid_configs = (
            {"additional-client-configuration": "[notclientsection]"},
            {"additional-client-configuration": "nakedkey"},
            {"additional-client-configuration": "globalkey = value"},
        )

        for invalid_config in invalid_configs:
            with self.assertRaises(ClientCharmError) as e:
                get_additional_client_configuration(invalid_config)
                self.assertIn(
                    invalid_config["additional-client-configuration"],
                    str(e),
                )

    def test_unknown_additional_config_key(self):
        """
        Additional config is not checked; it is passed as-is, including keys that are not
        meaningful to Landscape client.

        There is currently no schema validation for config.
        """
        juju_config = {
            "additional-client-configuration": "[client]\nsomevalue = somekey"
        }
        expected = {"somevalue": "somekey"}
        self.assertEqual(expected, get_additional_client_configuration(juju_config))
