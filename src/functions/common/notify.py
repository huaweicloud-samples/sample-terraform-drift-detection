"""告警与人工确认通知。

通知走消息通知服务的主订阅（邮件）。两类内容：

1. **需要确认的漂移**：列出高等级变更明细、巡检结果对象位置，以及
   运维可以执行的确认命令（命令文本由部署时下发的 ``APPROVE_COMMAND`` 给出，
   环境相关参数一律取自该配置，代码里不写死）。
2. **链路故障**：构建失败、取不到巡检结果、拿不到分级结论等，
   把已知的请求编号与状态一并带上，便于直接开工单。

邮件主题只用可打印 ASCII：主题会进入邮件头，含中文时部分收件端会拒收或乱码；
正文不受此限制，按运维阅读习惯用中文书写。
"""

from common.errors import ServiceError, TransportError

SUBJECT_APPROVAL = "Terraform Drift Approval Required"
SUBJECT_FAILURE = "Terraform Drift Pipeline Failed"
_MAX_LISTED_CHANGES = 20


class Notifier(object):
    """通知发送入口；发送失败不阻断主流程，只记日志并回报状态。"""

    def __init__(self, api, topic_urn, approve_command=None, logger=None):
        self.api = api
        self.topic_urn = topic_urn
        self.approve_command = approve_command
        self.logger = logger

    def send_drift_verdict(self, verdict, context=None):
        """发送「需要人工确认」的漂移通知。"""
        subject, message = build_drift_message(verdict, context, self.approve_command)
        return self._publish(subject, message)

    def send_failure(self, reason, context=None):
        """发送链路故障通知。"""
        subject, message = build_failure_message(reason, context)
        return self._publish(subject, message)

    def _publish(self, subject, message):
        if not self.topic_urn:
            if self.logger is not None:
                self.logger.warning("未配置告警主题，跳过通知发送")
            return {"sent": False, "reason": "topic not configured", "subject": subject}
        try:
            response = self.api.publish_message(self.topic_urn, subject, message)
        except (ServiceError, TransportError) as error:
            if self.logger is not None:
                self.logger.error("通知发送失败 detail=%s", error)
            return {
                "sent": False,
                "reason": str(error),
                "subject": subject,
                "message": message,
            }
        message_id = None
        if isinstance(response, dict):
            message_id = response.get("message_id") or response.get("msgId")
        if self.logger is not None:
            self.logger.info("通知已发送 subject=%s message_id=%s", subject, message_id)
        return {"sent": True, "subject": subject, "message_id": message_id}


def build_drift_message(verdict, context=None, approve_command=None):
    """拼装人工确认通知正文。"""
    context = context or {}
    severity = verdict.get("severity")
    counts = verdict.get("counts") or {}
    lines = [
        "巡检发现需要人工确认的配置差异。",
        "风险等级：%s" % severity,
        "差异条数：严重 %s / 高 %s / 低 %s"
        % (
            counts.get("critical", 0),
            counts.get("high", 0),
            counts.get("low", 0),
        ),
        "",
    ]
    lines.extend(_change_lines("严重变更", verdict.get("critical_changes") or []))
    lines.extend(_change_lines("高风险变更", verdict.get("high_changes") or []))

    lines.append("")
    lines.append("巡检结果对象：%s" % (context.get("plan_key") or "未提供"))
    build = context.get("build") or {}
    if build:
        lines.append(
            "巡检构建：任务 %s / 编号 %s / 状态 %s"
            % (build.get("job_id"), build.get("build_no"), build.get("status"))
        )
    if context.get("reports_bucket"):
        lines.append("报告桶：%s" % context["reports_bucket"])
    lines.append("")
    lines.append("确认修复的方式：")
    if approve_command:
        lines.append("  执行命令：%s" % approve_command)
    if context.get("apply_job_id"):
        lines.append("  或触发修复构建任务：%s" % context["apply_job_id"])
    lines.append("  未经确认不会自动改动上述资源。")
    return SUBJECT_APPROVAL, "\n".join(lines)


def build_failure_message(reason, context=None):
    """拼装链路故障通知正文。"""
    context = context or {}
    lines = ["漂移巡检链路执行失败，本次未产生修复动作。", "失败原因：%s" % reason]
    for key in ("plan_job_id", "plan_build_no", "apply_job_id", "apply_build_no", "plan_key", "stage"):
        value = context.get(key)
        if value:
            lines.append("%s：%s" % (key, value))
    for key in ("error", "detail", "status", "err_msg"):
        value = context.get(key)
        if value:
            lines.append("%s：%s" % (key, value))
    return SUBJECT_FAILURE, "\n".join(lines)


def _change_lines(title, changes):
    if not changes:
        return []
    lines = ["", "%s：" % title]
    for change in changes[:_MAX_LISTED_CHANGES]:
        actions = "/".join(change.get("actions") or []) or "变更"
        marker = "（涉及删除或替换）" if change.get("destructive") else ""
        lines.append("  - %s [%s]%s" % (change.get("address"), actions, marker))
        paths = change.get("replace_paths") or []
        if paths:
            lines.append("      需替换的属性：%s" % ", ".join(paths))
    if len(changes) > _MAX_LISTED_CHANGES:
        lines.append("  …… 其余 %d 条见巡检结果对象" % (len(changes) - _MAX_LISTED_CHANGES))
    return lines
