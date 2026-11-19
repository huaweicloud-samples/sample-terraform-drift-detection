# -*- coding: utf-8 -*-
"""配置合并与凭据获取校验。"""

import json
import unittest

from tests import support

from common import credentials as credentials_module
from common.config import Config, REQUIRED_KEYS
from common.errors import ConfigError

ENVIRON_CREDENTIALS = {
    "HW_ACCESS_KEY": support.PLACEHOLDER_ACCESS_KEY,
    "HW_SECRET_KEY": support.PLACEHOLDER_SECRET_KEY,
    "HW_SECURITY_TOKEN": support.PLACEHOLDER_SECURITY_TOKEN,
    "HW_PROJECT_ID": "project-id-example",
}


class ConfigSourceTest(unittest.TestCase):
    def test_dict_user_data_is_read(self):
        context = support.FakeContext(user_data=support.CLASSIFIER_USER_DATA)
        config = Config.load(context=context, role="classifier", event={}, environ={})
        config.validate()
        self.assertEqual("drift_detection.custom", config.get("CES_NAMESPACE"))

    def test_json_string_user_data_is_read(self):
        context = support.FakeContext(user_data=support.CLASSIFIER_USER_DATA, as_json_string=True)
        config = Config.load(context=context, role="classifier", event={}, environ={})
        self.assertEqual("plans", config.get("PLAN_KEY_PREFIX"))

    def test_wrapped_user_data_is_read(self):
        context = support.FakeContext(
            user_data=support.CLASSIFIER_USER_DATA, as_json_string=True, wrapped=True
        )
        config = Config.load(context=context, role="classifier", event={}, environ={})
        self.assertEqual("tf-drift-reports-example", config.get("REPORTS_BUCKET"))

    def test_event_overrides_user_data(self):
        context = support.FakeContext(user_data=support.ORCHESTRATOR_USER_DATA)
        event = {"wait_seconds": 5, "poll_interval": 1, "plan_job_id": "job-override"}
        config = Config.load(context=context, role="orchestrator", event=event, environ={})
        self.assertEqual(5, config.int_of("WAIT_SECONDS"))
        self.assertEqual(1, config.int_of("POLL_INTERVAL"))
        self.assertEqual("job-override", config.get("PLAN_JOB_ID"))

    def test_environment_is_the_lowest_priority(self):
        context = support.FakeContext(user_data={"CES_NAMESPACE": "from_user_data.custom"})
        config = Config.load(
            context=context, role="classifier", event={},
            environ=dict(ENVIRON_CREDENTIALS, CES_NAMESPACE="from_env.custom"),
        )
        self.assertEqual("from_user_data.custom", config.get("CES_NAMESPACE"))

    def test_missing_keys_are_reported_together(self):
        config = Config(values={"HW_REGION": "cn-north-4"}, role="classifier")
        with self.assertRaises(ConfigError) as caught:
            config.validate()
        missing = caught.exception.context["missing"]
        self.assertIn("CES_NAMESPACE", missing)
        self.assertNotIn("HW_REGION", missing)

    def test_empty_value_counts_as_missing(self):
        values = dict(support.CLASSIFIER_USER_DATA, CES_DIMENSION="")
        config = Config(values=values, role="classifier")
        with self.assertRaises(ConfigError):
            config.validate()

    def test_int_conversion_errors_are_explicit(self):
        values = dict(support.ORCHESTRATOR_USER_DATA, WAIT_SECONDS="PT5M")
        config = Config(values=values, role="orchestrator")
        with self.assertRaises(ConfigError):
            config.int_of("WAIT_SECONDS")

    def test_snapshot_masks_secrets(self):
        values = dict(support.CLASSIFIER_USER_DATA, **ENVIRON_CREDENTIALS)
        snapshot = Config(values=values, role="classifier").snapshot()
        self.assertEqual("<masked>", snapshot["HW_SECRET_KEY"])
        self.assertEqual("<masked>", snapshot["HW_ACCESS_KEY"])
        self.assertEqual("<masked>", snapshot["HW_SECURITY_TOKEN"])
        self.assertEqual("cn-north-4", snapshot["HW_REGION"])

    def test_required_key_sets_cover_every_declared_key(self):
        """必填项覆盖基础设施声明的全部键，不留未使用的下发项。"""
        from tests.test_infra_contract import declared_keys

        self.assertEqual(
            set(REQUIRED_KEYS["orchestrator"]), declared_keys("orchestrator_user_data")
        )
        self.assertEqual(set(REQUIRED_KEYS["classifier"]), declared_keys("classifier_user_data"))


class CredentialsTest(unittest.TestCase):
    def test_context_credentials_win(self):
        creds = credentials_module.resolve(context=support.FakeContext(), environ={
            "HW_ACCESS_KEY": "AK-from-env", "HW_SECRET_KEY": "SK-from-env"
        })
        self.assertEqual(support.PLACEHOLDER_ACCESS_KEY, creds.access_key)
        self.assertEqual("context", creds.source)

    def test_environment_fallback(self):
        creds = credentials_module.resolve(context=None, environ=ENVIRON_CREDENTIALS)
        self.assertEqual(support.PLACEHOLDER_ACCESS_KEY, creds.access_key)
        self.assertEqual(support.PLACEHOLDER_SECURITY_TOKEN, creds.security_token)
        self.assertEqual("project-id-example", creds.project_id)
        self.assertEqual("environment", creds.source)

    def test_missing_credentials_raise(self):
        with self.assertRaises(ConfigError) as caught:
            credentials_module.resolve(context=None, environ={})
        self.assertEqual(["access key", "secret key"], caught.exception.context["missing"])

    def test_context_returning_none_falls_back_to_environment(self):
        class EmptyContext(object):
            def getSecurityAccessKey(self):
                return None

            def getSecuritySecretKey(self):
                return ""

        creds = credentials_module.resolve(context=EmptyContext(), environ=ENVIRON_CREDENTIALS)
        self.assertEqual("environment", creds.source)

    def test_redacted_view_has_no_secrets(self):
        creds = credentials_module.resolve(context=None, environ=ENVIRON_CREDENTIALS)
        redacted = json.dumps(creds.redacted(), ensure_ascii=False)
        self.assertNotIn(support.PLACEHOLDER_SECRET_KEY, redacted)
        self.assertNotIn(support.PLACEHOLDER_SECURITY_TOKEN, redacted)
        self.assertNotIn(support.PLACEHOLDER_ACCESS_KEY, redacted)
        self.assertIn("project-id-example", redacted)

    def test_project_id_from_urn(self):
        urn = "urn:fss:cn-north-4:abc123def:function:default:tf-drift-classifier:latest"
        self.assertEqual("abc123def", credentials_module.project_id_from_urn(urn))
        self.assertIsNone(credentials_module.project_id_from_urn(None))
        self.assertIsNone(credentials_module.project_id_from_urn("not-an-urn"))

    def test_topic_urn_is_not_mistaken_for_function_urn(self):
        """消息主题 URN 的项目段与函数 URN 同位，反解出来同为区域项目 ID。"""
        self.assertEqual(
            "project-id-example",
            credentials_module.project_id_from_urn(support.ORCHESTRATOR_USER_DATA["SMN_TOPIC_URN"]),
        )


if __name__ == "__main__":
    unittest.main()
