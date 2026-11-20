# -*- coding: utf-8 -*-
"""巡检结果解析与归一化校验。"""

import unittest

from tests import support
from common import plan_parser


class PlanJsonDocumentTest(unittest.TestCase):
    """``terraform show -json`` 的计划输出。"""

    def setUp(self):
        self.insight = support.load_insight("plan_with_drift.json")

    def test_format_and_drift_flag(self):
        self.assertEqual("plan_json", self.insight.format)
        self.assertTrue(self.insight.drift_detected)

    def test_all_entries_are_normalized(self):
        expected_fields = [
            "action",
            "actions",
            "address",
            "after",
            "before",
            "mode",
            "name",
            "provider",
            "replace_paths",
            "source",
            "type",
        ]
        self.assertEqual(6, len(self.insight.changes))
        for change in self.insight.changes:
            self.assertEqual(expected_fields, sorted(change.keys()))
            self.assertEqual("plan_json", change["source"])

    def test_data_source_and_no_op_are_not_actionable(self):
        actionable = self.insight.actionable_changes()
        addresses = [change["address"] for change in actionable]
        self.assertEqual(4, len(actionable))
        self.assertNotIn("data.huaweicloud_availability_zones.names", addresses)
        self.assertNotIn("huaweicloud_obs_bucket.reports", addresses)

    def test_replace_is_collapsed_with_replace_paths(self):
        policy = [
            change
            for change in self.insight.changes
            if change["type"] == "huaweicloud_obs_bucket_policy"
        ][0]
        self.assertEqual("replace", policy["action"])
        self.assertTrue(plan_parser.destructive(policy))
        self.assertEqual([["policy"]], policy["replace_paths"])

    def test_address_falls_back_to_type_and_name(self):
        change = plan_parser.normalize({"change": {"actions": ["update"]}})
        self.assertEqual("unknown", change["address"])
        self.assertEqual("update", change["action"])

    def test_module_address_keeps_last_two_segments(self):
        change = plan_parser.normalize(
            {
                "address": "module.pipeline.huaweicloud_fgs_function.orchestrator",
                "change": {"actions": ["update"], "before": {}, "after": {}},
            }
        )
        self.assertEqual("huaweicloud_fgs_function", change["type"])
        self.assertEqual("orchestrator", change["name"])

    def test_document_can_be_nested_one_level(self):
        wrapped = {"result": support.fixture_json("no_drift.json")}
        insight = plan_parser.load(wrapped)
        self.assertEqual("summary", insight.format)
        self.assertFalse(insight.drift_detected)

    def test_diagnostics_are_collected(self):
        insight = support.load_insight("critical_only.json")
        self.assertEqual(1, len(insight.diagnostics))
        self.assertIn("资源属性由外部改动", insight.diagnostics[0])


class NoDriftSummaryTest(unittest.TestCase):
    """构建脚本的「无差异」摘要分支。"""

    def test_summary_is_recognised(self):
        insight = support.load_insight("no_drift.json")
        self.assertEqual("summary", insight.format)
        self.assertFalse(insight.drift_detected)
        self.assertEqual([], insight.actionable_changes())
        self.assertEqual([], insight.diagnostics)


class StateDriftTest(unittest.TestCase):
    """仅状态刷新发现的差异也要计入。"""

    def test_resource_drift_entries_are_converted(self):
        insight = support.load_insight("state_drift_only.json")
        self.assertEqual(2, len(insight.changes))
        self.assertTrue(all(change["source"] == "resource_drift" for change in insight.changes))
        self.assertTrue(insight.drift_detected)
        self.assertEqual(2, len(insight.actionable_changes()))


class StreamJsonTest(unittest.TestCase):
    """``terraform plan -json`` 的逐行事件流。"""

    def setUp(self):
        self.insight = support.load_insight("plan_stream.jsonl")

    def test_format_and_changes(self):
        self.assertEqual("stream_json", self.insight.format)
        self.assertTrue(self.insight.drift_detected)
        self.assertEqual(3, len(self.insight.actionable_changes()))

    def test_action_names_are_mapped(self):
        by_type = {change["type"]: change for change in self.insight.changes}
        self.assertEqual("delete", by_type["huaweicloud_networking_secgroup_rule"]["action"])
        self.assertEqual("replace", by_type["huaweicloud_obs_bucket"]["action"])
        self.assertEqual("update", by_type["huaweicloud_dms_kafka_instance"]["action"])

    def test_before_after_fields_are_mapped(self):
        rule = [
            change
            for change in self.insight.changes
            if change["type"] == "huaweicloud_networking_secgroup_rule"
        ][0]
        self.assertEqual({"port": "443", "cidr": "0.0.0.0/0"}, rule["before"])
        self.assertIsNone(rule["after"])

    def test_summaries_and_diagnostics(self):
        self.assertEqual(1, len(self.insight.diagnostics))
        self.assertIn("替换", self.insight.diagnostics[0])
        self.assertEqual(
            {"resource": {"add": 0, "change": 2, "remove": 1, "import": 0, "operate": 3}},
            self.insight.summary["change_summary"],
        )
        self.assertIn("apply_summary", self.insight.summary)

    def test_module_prefixed_address_is_resolved(self):
        bucket = [
            change
            for change in self.insight.changes
            if change["type"] == "huaweicloud_obs_bucket"
        ][0]
        self.assertEqual("module.pipeline.huaweicloud_obs_bucket.reports", bucket["address"])


class DriftInferredFromSummaryTest(unittest.TestCase):
    """逐行流里没有单条变更事件时，按汇总计数判定。"""

    def test_change_summary_counts_as_drift(self):
        text = (
            '{"type":"change_summary","change_summary":'
            '{"resource":{"add":0,"change":1,"remove":0,"operate":1}}}\n'
        )
        insight = plan_parser.load(text)
        self.assertEqual("stream_json", insight.format)
        self.assertTrue(insight.drift_detected)
        self.assertEqual([], insight.changes)

    def test_all_zero_summary_is_no_drift(self):
        text = (
            '{"type":"change_summary","change_summary":'
            '{"resource":{"add":0,"change":0,"remove":0,"operate":0}}}\n'
        )
        self.assertFalse(plan_parser.load(text).drift_detected)


class InputHandlingTest(unittest.TestCase):
    """各种输入形态都要能落地，不能抛异常。"""

    def test_bytes_input(self):
        insight = plan_parser.load(support.fixture_text("no_drift.json").encode("utf-8"))
        self.assertEqual("summary", insight.format)

    def test_dict_input(self):
        insight = plan_parser.load(support.fixture_json("no_drift.json"))
        self.assertFalse(insight.drift_detected)

    def test_empty_input(self):
        for value in (None, "", b"", "   "):
            insight = plan_parser.load(value)
            self.assertIn(insight.format, ("empty", "unknown"))

    def test_plain_text_is_reported_as_unparseable(self):
        insight = support.load_insight("human_readable_plan.txt")
        self.assertEqual("unknown", insight.format)
        self.assertTrue(insight.diagnostics)

    def test_bare_list_document(self):
        insight = plan_parser.load(
            [
                {
                    "address": "huaweicloud_vpc.main",
                    "mode": "managed",
                    "type": "huaweicloud_vpc",
                    "name": "main",
                    "change": {"actions": ["update"], "before": {"cidr": "10.0.0.0/16"}, "after": {"cidr": "10.1.0.0/16"}},
                }
            ]
        )
        self.assertEqual(1, len(insight.actionable_changes()))
        self.assertTrue(insight.drift_detected)


class ActionHelperTest(unittest.TestCase):
    """折叠与判定辅助函数的边界。"""

    def test_collapse_action(self):
        cases = [
            ([], "none"),
            (["no-op"], "none"),
            (["no-op", "no-op"], "none"),
            (["create"], "create"),
            (["delete"], "delete"),
            (["delete", "create"], "replace"),
            (["create", "delete"], "replace"),
            (["update"], "update"),
            (["import"], "import"),
            (["move"], "move"),
            (["read"], "none"),
        ]
        for actions, expected in cases:
            self.assertEqual(expected, plan_parser.collapse_action(actions), actions)

    def test_is_actionable(self):
        self.assertFalse(plan_parser.is_actionable({"mode": "data", "actions": ["create"]}))
        self.assertFalse(plan_parser.is_actionable({"mode": "managed", "actions": ["no-op"]}))
        self.assertFalse(plan_parser.is_actionable({"mode": "managed", "actions": []}))
        self.assertFalse(plan_parser.is_actionable("not a dict"))
        self.assertTrue(plan_parser.is_actionable({"mode": "managed", "actions": ["update"]}))

    def test_destructive(self):
        self.assertTrue(plan_parser.destructive({"action": "delete"}))
        self.assertTrue(plan_parser.destructive({"action": "replace"}))
        self.assertFalse(plan_parser.destructive({"action": "update"}))

    def test_to_dict_shape(self):
        payload = support.load_insight("plan_with_drift.json").to_dict()
        self.assertEqual(
            [
                "actionable_count",
                "change_count",
                "diagnostics",
                "drift_detected",
                "plan_format",
                "summary",
            ],
            sorted(payload.keys()),
        )
        self.assertEqual(4, payload["actionable_count"])


if __name__ == "__main__":
    unittest.main()
