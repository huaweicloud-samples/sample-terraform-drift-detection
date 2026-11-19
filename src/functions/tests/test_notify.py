# -*- coding: utf-8 -*-
"""通知文案与发送失败处理校验。"""

import unittest

from tests import support

from common.errors import ServiceError
from common.notify import (
    SUBJECT_APPROVAL,
    SUBJECT_FAILURE,
    Notifier,
    build_drift_message,
    build_failure_message,
)


class MessageContentTest(unittest.TestCase):
    def setUp(self):
        self.verdict = support.verdict_of("plan_with_drift.json")
        self.context = {
            "plan_key": "plans/20260101T000000Z/drift_result.json",
            "reports_bucket": "tf-drift-reports-example",
            "apply_job_id": "job-apply-0002",
            "build": {"job_id": "job-plan-0001", "build_no": "1001", "status": "success"},
        }

    def test_subject_is_printable_ascii(self):
        """主题进入邮件头，含非 ASCII 时部分收件端会拒收。"""
        for subject in (SUBJECT_APPROVAL, SUBJECT_FAILURE):
            self.assertTrue(subject.isascii(), subject)
            self.assertTrue(subject.isprintable(), subject)

    def test_drift_message_lists_changes_and_approval_path(self):
        _, message = build_drift_message(self.verdict, self.context, "terraform apply -auto-approve")
        self.assertIn("critical", message)
        self.assertIn("huaweicloud_networking_secgroup_rule.allow_any_ssh", message)
        self.assertIn("terraform apply -auto-approve", message)
        self.assertIn("job-apply-0002", message)
        self.assertIn(self.context["plan_key"], message)
        self.assertIn("tf-drift-reports-example", message)
        self.assertIn("1001", message)
        self.assertIn("未经确认不会自动改动上述资源", message)

    def test_replace_paths_are_called_out(self):
        _, message = build_drift_message(self.verdict, self.context, None)
        self.assertIn("需替换的属性：policy", message)

    def test_missing_context_does_not_break_message(self):
        subject, message = build_drift_message(self.verdict, None, None)
        self.assertEqual(SUBJECT_APPROVAL, subject)
        self.assertIn("未提供", message)

    def test_long_change_list_is_truncated(self):
        changes = [
            {
                "address": "huaweicloud_networking_secgroup.rule-%02d" % index,
                "type": "huaweicloud_networking_secgroup",
                "name": "rule-%02d" % index,
                "actions": ["update"],
                "action": "update",
                "destructive": False,
                "replace_paths": [],
            }
            for index in range(25)
        ]
        verdict = {
            "severity": "critical",
            "action": "notify_and_approve",
            "message": "共 25 项差异",
            "counts": {"critical": 25, "high": 0, "low": 0, "total": 25},
            "critical_changes": changes,
            "high_changes": [],
            "low_changes": [],
        }
        _, message = build_drift_message(verdict, {}, "terraform apply")
        self.assertIn("其余 5 条见巡检结果对象", message)
        self.assertEqual(20, message.count("huaweicloud_networking_secgroup.rule-"))

    def test_failure_message_carries_stage_and_builds(self):
        subject, message = build_failure_message(
            "巡检链路在「plan_build」阶段失败",
            {
                "stage": "plan_build",
                "plan_job_id": "job-plan-0001",
                "plan_build_no": "1001",
                "err_msg": "terraform plan 退出码 1",
            },
        )
        self.assertEqual(SUBJECT_FAILURE, subject)
        self.assertIn("job-plan-0001", message)
        self.assertIn("1001", message)
        self.assertIn("terraform plan 退出码 1", message)


class NotifierTest(unittest.TestCase):
    def test_success_returns_message_id(self):
        api = support.FakeApi()
        result = Notifier(api, support.ORCHESTRATOR_USER_DATA["SMN_TOPIC_URN"]).send_drift_verdict(
            self_verdict(), {"plan_key": "k"}
        )
        self.assertTrue(result["sent"])
        self.assertEqual("msg-fake-1", result["message_id"])
        self.assertEqual(1, api.count("publish_message"))
        self.assertEqual(SUBJECT_APPROVAL, api.published[0]["subject"])

    def test_missing_topic_skips_send(self):
        result = Notifier(support.FakeApi(), None).send_failure("链路故障", {})
        self.assertFalse(result["sent"])
        self.assertEqual("topic not configured", result["reason"])

    def test_publish_failure_is_swallowed(self):
        """通知失败不能改写巡检结论，只回报状态与正文。"""
        api = support.FakeApi(publish_error=ServiceError("主题无订阅", status_code=400))
        result = Notifier(api, "urn:smn:cn-north-4:p:t").send_failure("链路故障", {"stage": "act"})
        self.assertFalse(result["sent"])
        self.assertIn("链路故障", result["message"])

    def test_approve_command_comes_from_configuration(self):
        api = support.FakeApi()
        Notifier(api, "urn:smn:cn-north-4:p:t", approve_command="bash buildscripts/apply.sh").send_drift_verdict(
            self_verdict(), {}
        )
        self.assertIn("bash buildscripts/apply.sh", api.published[0]["message"])


def self_verdict():
    return support.verdict_of("critical_only.json")


if __name__ == "__main__":
    unittest.main()
