# -*- coding: utf-8 -*-
"""风险分级规则校验：等级判定、动作建议与分组明细。"""

import unittest

from tests import support

from common import classification


def _change(resource_type, before, after, actions=None):
    return {
        "address": "%s.demo" % resource_type,
        "type": resource_type,
        "name": "demo",
        "mode": "managed",
        "provider": "",
        "actions": actions or ["update"],
        "action": "update",
        "before": before,
        "after": after,
        "replace_paths": [],
        "source": "plan_json",
    }


class SeverityTest(unittest.TestCase):
    def test_security_boundary_is_critical(self):
        for resource_type in (
            "huaweicloud_networking_secgroup",
            "huaweicloud_networking_secgroup_rule",
            "huaweicloud_obs_bucket_policy",
            "huaweicloud_obs_bucket_bpa",
        ):
            change = _change(resource_type, {"a": 1}, {"a": 2})
            self.assertEqual("critical", classification.severity_of(change), resource_type)

    def test_access_control_and_encryption_are_critical(self):
        for resource_type in (
            "huaweicloud_identity_role",
            "huaweicloud_identity_group_role_assignment",
            "huaweicloud_identity_agency",
            "huaweicloud_kms_key",
            "huaweicloud_kms_grant",
        ):
            change = _change(resource_type, {"a": 1}, {"a": 2})
            self.assertEqual("critical", classification.severity_of(change), resource_type)

    def test_pipeline_and_business_resources_are_high(self):
        for resource_type in (
            "huaweicloud_obs_bucket",
            "huaweicloud_fgs_function",
            "huaweicloud_fgs_trigger",
            "huaweicloud_codearts_build_task",
            "huaweicloud_lts_stream",
            "huaweicloud_smn_topic",
            "huaweicloud_vpc",
            "huaweicloud_compute_instance",
        ):
            change = _change(resource_type, {"a": 1}, {"a": 2})
            self.assertEqual("high", classification.severity_of(change), resource_type)

    def test_unknown_type_defaults_to_high(self):
        change = _change("huaweicloud_sfs_turbo", {"a": 1}, {"a": 2})
        self.assertEqual("high", classification.severity_of(change))

    def test_tag_only_change_on_unknown_type_is_low(self):
        change = _change(
            "huaweicloud_fgs_dependency",
            {"dependency_type": 1, "version": 2, "tags": {"Owner": "ops"}},
            {"dependency_type": 1, "version": 2, "tags": {"Owner": "sre"}},
        )
        self.assertEqual("low", classification.severity_of(change))

    def test_tag_only_change_on_high_risk_type_stays_high(self):
        """安全边界与业务资源不因「只改标签」而被判为低风险。"""
        change = _change("huaweicloud_obs_bucket", {"tags": {"a": "1"}}, {"tags": {"a": "2"}})
        self.assertEqual("high", classification.severity_of(change))


class MetadataOnlyTest(unittest.TestCase):
    def test_null_attributes_are_not_treated_as_changes(self):
        """华为云计划输出把未赋值属性写成 null，比较前需剔除。"""
        change = _change(
            "huaweicloud_cts_tracker",
            {"bucket_name": "audit", "tags": {"Env": "prod"}, "description": None},
            {"bucket_name": "audit", "tags": {"Env": "dev"}, "description": None},
        )
        self.assertTrue(classification.is_metadata_only(change))

    def test_real_attribute_change_is_not_metadata_only(self):
        change = _change(
            "huaweicloud_cts_tracker",
            {"bucket_name": "audit", "tags": {"Env": "prod"}},
            {"bucket_name": "audit-2", "tags": {"Env": "prod"}},
        )
        self.assertFalse(classification.is_metadata_only(change))

    def test_identical_before_after_is_metadata_only(self):
        change = _change("huaweicloud_anything", {"a": 1}, {"a": 1})
        self.assertTrue(classification.is_metadata_only(change))


class ClassifyResultTest(unittest.TestCase):
    def test_empty_input(self):
        verdict = classification.classify([])
        self.assertEqual("none", verdict["severity"])
        self.assertEqual("none", verdict["action"])
        self.assertEqual({"critical": 0, "high": 0, "low": 0, "total": 0}, verdict["counts"])

    def test_low_only_triggers_auto_remediation(self):
        verdict = support.verdict_of("low_risk_only.json")
        self.assertEqual("low", verdict["severity"])
        self.assertEqual("auto_remediate", verdict["action"])
        self.assertEqual(2, verdict["counts"]["total"])

    def test_critical_present_forces_approval(self):
        verdict = support.verdict_of("critical_only.json")
        self.assertEqual("critical", verdict["severity"])
        self.assertEqual("notify_and_approve", verdict["action"])
        self.assertEqual(2, verdict["counts"]["critical"])

    def test_mixed_input_reports_highest_severity(self):
        verdict = support.verdict_of("plan_with_drift.json")
        self.assertEqual("critical", verdict["severity"])
        self.assertEqual("notify_and_approve", verdict["action"])
        self.assertEqual(
            {"critical": 2, "high": 1, "low": 1, "total": 4},
            verdict["counts"],
        )
        self.assertEqual(
            ["huaweicloud_networking_secgroup_rule", "huaweicloud_obs_bucket_policy"],
            sorted(item["type"] for item in verdict["critical_changes"]),
        )

    def test_non_actionable_entries_are_skipped(self):
        data_source = _change("huaweicloud_vpc", {}, {}, actions=["read"])
        data_source["mode"] = "data"
        verdict = classification.classify([data_source])
        self.assertEqual("none", verdict["severity"])

    def test_entry_shape(self):
        verdict = support.verdict_of("plan_with_drift.json")
        for item in verdict["critical_changes"] + verdict["high_changes"] + verdict["low_changes"]:
            self.assertEqual(
                [
                    "action",
                    "actions",
                    "address",
                    "destructive",
                    "name",
                    "replace_paths",
                    "type",
                ],
                sorted(item.keys()),
            )

    def test_replace_paths_are_flattened(self):
        verdict = support.verdict_of("plan_with_drift.json")
        policy = [
            item for item in verdict["critical_changes"] if item["type"] == "huaweicloud_obs_bucket_policy"
        ][0]
        self.assertEqual(["policy"], policy["replace_paths"])
        self.assertTrue(policy["destructive"])

    def test_message_counts_match_groups(self):
        verdict = support.verdict_of("plan_with_drift.json")
        counts = verdict["counts"]
        self.assertIn(str(counts["total"]), verdict["message"])
        self.assertIn(str(counts["critical"]), verdict["message"])


class StreamVerdictTest(unittest.TestCase):
    """逐行 JSON 流与计划 JSON 判出的等级必须一致。"""

    def test_stream_fixture_severity(self):
        verdict = support.verdict_of("plan_stream.jsonl")
        self.assertEqual("critical", verdict["severity"])
        self.assertEqual(1, verdict["counts"]["critical"])
        self.assertEqual(1, verdict["counts"]["high"])
        self.assertEqual(1, verdict["counts"]["low"])

    def test_refresh_drift_fixture_severity(self):
        verdict = support.verdict_of("state_drift_only.json")
        self.assertEqual("high", verdict["severity"])
        self.assertEqual("notify_and_approve", verdict["action"])

    def test_no_drift_fixture_severity(self):
        verdict = support.verdict_of("no_drift.json")
        self.assertEqual("none", verdict["severity"])


if __name__ == "__main__":
    unittest.main()
