"""巡检编排函数（FunctionGraph Python 运行时入口）。

职责：把「一次巡检」串成确定性的顺序流程，并在结论出来后决定是否自动修复。

::

    定时触发
       ↓
    触发巡检构建（terraform plan）      POST /v3/jobs/build + 轮询 record-info
       ↓
    从报告桶取最新巡检结果              plans/<时间戳>/drift_result.json
       ↓
    同步调用分级函数判风险              POST /v2/{project}/fgs/functions/{urn}/invocations
       ↓
    ┌── 无差异 ────────────────────────────→ 结束（只上报指标，由分级函数完成）
    ├── 仅低风险差异 ──────────────────────→ 触发修复构建（terraform apply）并等待终态
    └── 存在严重/高风险差异 ───────────────→ 发人工确认通知，不改动任何资源

运行期配置全部来自用户数据（键名见 ``infra/locals.tf``），本文件不新造键名：
``PLAN_JOB_ID``、``APPLY_JOB_ID``、``CLASSIFIER_URN``、``REPORTS_BUCKET``、
``PLAN_KEY_PREFIX``、``OBS_ENDPOINT``、``SMN_TOPIC_URN``、``CES_NAMESPACE``、
``CES_DIMENSION``、``CES_DIMENSION_VALUE``、``HW_REGION``、``WAIT_SECONDS``、
``POLL_INTERVAL``、``APPROVE_COMMAND``。

事件字段（全部可选，缺省即按定时触发执行）::

    {
      "plan_key":  "plans/20260101T000000Z/drift_result.json",  # 直接指定巡检结果对象
      "classification": {...},                                   # 注入分级结论，用于演练
      "dry_run": true,                                           # 只巡检，不修复也不通知
      "wait_seconds": 60, "poll_interval": 5,                    # 缩短等待，用于验证
      "plan_job_id": "...", "apply_job_id": "..."                # 临时改用其它构建任务
    }

失败上报：任何阶段异常都会先向告警主题发一条故障通知（含阶段名、构建编号、
请求编号），随后抛出，让函数执行记录为失败并进入日志流；
异步调用失败另有基础设施声明的兜底投递。
"""

import json
import time

from common import runtime as runtime_module
from common.errors import (
    BuildFailedError,
    PipelineError,
    PipelineRuntimeError,
)
from common.notify import Notifier, build_drift_message

ROLE = "orchestrator"

DEFAULT_WAIT_SECONDS = 900
DEFAULT_POLL_INTERVAL_SECONDS = 15
MIN_POLL_INTERVAL_SECONDS = 1


def main_handler(event, context):
    """函数入口。

    :param event: 触发事件，定时触发器传入的是计时器信息，其余字段可覆盖默认行为
    :param context: 运行上下文，提供临时凭据与用户数据
    """
    # 入口先打印一行，确保「代码没挂载 / 依赖缺失」这类问题在日志里可见
    print("=== HANDLER ENTRY: orchestrator ===", flush=True)
    rt = runtime_module.build(ROLE, event, context)
    flow = ScanFlow(rt)
    return flow.run(event if isinstance(event, dict) else {})


class ScanFlow(object):
    """一次巡检的编排逻辑。"""

    def __init__(self, runtime, notifier=None):
        self.runtime = runtime
        self.config = runtime.config
        self.api = runtime.api
        self.store = runtime.store
        self.logger = runtime.logger
        self.sleep = time.sleep
        self.notifier = notifier or Notifier(
            api=self.api,
            topic_urn=self.config.get("SMN_TOPIC_URN"),
            approve_command=self.config.get("APPROVE_COMMAND"),
            logger=self.logger,
        )
        self.stage = "init"
        self.plan_build = None
        self.apply_build = None
        self.plan_key = None

    # ------------------------------------------------------------------
    def run(self, event):
        started = time.monotonic()
        event = event or {}
        dry_run = bool(event.get("dry_run"))
        result = {"role": ROLE, "dry_run": dry_run, "stages": []}

        try:
            self.stage = "plan_build"
            self.plan_build = self._run_build(
                self.config.require("PLAN_JOB_ID"), event, label="巡检"
            )
            result["stages"].append(self.stage)

            self.stage = "read_plan"
            self.plan_key = self._locate_plan(event)
            result["stages"].append(self.stage)

            self.stage = "classify"
            verdict = self._classify(event)
            result["stages"].append(self.stage)

            self.stage = "act"
            action_result = self._act(verdict, dry_run)
            result["stages"].append(self.stage)

            result.update(
                {
                    "status": action_result["status"],
                    "severity": verdict.get("severity"),
                    "action": verdict.get("action"),
                    "message": verdict.get("message"),
                    "classification": verdict,
                    "notification": action_result.get("notification"),
                }
            )
        except PipelineRuntimeError as error:
            self.logger.error("巡检在 %s 阶段失败：%s", self.stage, error)
            result.update({"status": "failed", "failed_stage": self.stage, "error": error.to_dict()})
            self._report_failure(result)
            raise
        finally:
            result["builds"] = {"plan": self.plan_build, "apply": self.apply_build}
            result["plan_key"] = self.plan_key
            result["duration_ms"] = int((time.monotonic() - started) * 1000)

        self.logger.info(
            "巡检结束 status=%s severity=%s message=%s",
            result.get("status"),
            result.get("severity"),
            result.get("message"),
        )
        return result

    # ------------------------------------------------------------------
    # 各阶段
    # ------------------------------------------------------------------
    def _run_build(self, job_id, event, label):
        """触发一次构建并等待终态。"""
        try:
            build_no = self.api.start_build(job_id)
        except PipelineRuntimeError as error:
            # 触发构建失败的响应体是定位唯一线索（错误码 / 字段名），补进异常上下文
            body = getattr(error, "body", None)
            raise PipelineError(
                "%s构建触发失败" % label,
                job_id=job_id,
                status_code=getattr(error, "status_code", None),
                service_body=(body or "")[:600],
            )
        self.logger.info("已触发%s构建 job_id=%s build_no=%s", label, job_id, build_no)
        build = self._wait_for_build(job_id, build_no, event)
        if not self.api.is_success(build):
            raise BuildFailedError(
                "%s构建未成功" % label,
                job_id=job_id,
                build_no=build_no,
                status=build.get("status"),
                err_msg=build.get("err_msg"),
            )
        return build

    def _wait_for_build(self, job_id, build_no, event=None):
        event = event or {}
        wait_seconds = int(
            event.get("wait_seconds")
            or self.config.int_of("WAIT_SECONDS", DEFAULT_WAIT_SECONDS)
            or DEFAULT_WAIT_SECONDS
        )
        poll_interval = float(
            event.get("poll_interval")
            or self.config.int_of("POLL_INTERVAL", DEFAULT_POLL_INTERVAL_SECONDS)
            or DEFAULT_POLL_INTERVAL_SECONDS
        )
        poll_interval = max(poll_interval, MIN_POLL_INTERVAL_SECONDS)

        deadline = time.monotonic() + max(wait_seconds, poll_interval)
        latest = {"job_id": job_id, "build_no": str(build_no), "status": ""}
        while True:
            latest = self.api.describe_build(job_id, build_no)
            if self.api.is_terminal(latest):
                self.logger.info(
                    "构建结束 job_id=%s build_no=%s status=%s duration=%s",
                    job_id,
                    build_no,
                    latest.get("status"),
                    latest.get("duration"),
                )
                return latest
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PipelineError(
                    "等待构建结束超时",
                    job_id=job_id,
                    build_no=build_no,
                    last_status=latest.get("status"),
                    wait_seconds=wait_seconds,
                )
            self.sleep(min(poll_interval, remaining))

    def _locate_plan(self, event):
        """定位本次要判读的巡检结果对象。"""
        explicit = event.get("plan_key")
        if explicit:
            self.logger.info("使用事件指定的巡检结果对象 key=%s", explicit)
            return str(explicit)

        prefix = "%s/" % self.config.require("PLAN_KEY_PREFIX").rstrip("/")
        key = self.store.latest_result_key(prefix)
        if not key:
            raise PipelineError(
                "报告桶内没有可判读的巡检结果",
                reports_bucket=self.config.get("REPORTS_BUCKET"),
                prefix=prefix,
            )
        self.logger.info("取到最新巡检结果对象 key=%s", key)
        return key

    def _classify(self, event):
        """调用分级函数并校验其返回结构。"""
        injected = event.get("classification")
        if isinstance(injected, dict) and "severity" in injected and "action" in injected:
            self.logger.info("使用事件注入的分级结论（演练模式）")
            return injected

        payload = {
            "reports_bucket": self.config.get("REPORTS_BUCKET"),
            "plan_key": self.plan_key,
            "trigger": ROLE,
            "build": {
                "job_id": (self.plan_build or {}).get("job_id"),
                "build_no": (self.plan_build or {}).get("build_no"),
                "status": (self.plan_build or {}).get("status"),
            },
            "metric": {
                "namespace": self.config.get("CES_NAMESPACE"),
                "dimension_name": self.config.get("CES_DIMENSION"),
                "dimension_value": self.config.get("CES_DIMENSION_VALUE"),
            },
        }
        response = self.api.invoke_function_sync(self.config.require("CLASSIFIER_URN"), payload)
        verdict = _as_dict(response)
        if "severity" not in verdict or "action" not in verdict:
            raise PipelineError(
                "分级函数返回结构不完整",
                classifier_urn=self.config.get("CLASSIFIER_URN"),
                response=_truncate(verdict),
            )
        return verdict

    def _act(self, verdict, dry_run):
        """按分级结论执行动作。"""
        action = str(verdict.get("action") or "")
        if action == "none":
            return {"status": "no_drift"}
        if action == "auto_remediate":
            if dry_run:
                self.logger.info("演练模式：跳过修复构建")
                return {"status": "would_remediate"}
            self.apply_build = self._run_build(
                self.config.require("APPLY_JOB_ID"), {}, label="修复"
            )
            return {"status": "remediated"}
        if action == "notify_and_approve":
            context = {
                "plan_key": self.plan_key,
                "reports_bucket": self.config.get("REPORTS_BUCKET"),
                "apply_job_id": self.config.get("APPLY_JOB_ID"),
                "build": self.plan_build,
            }
            if dry_run:
                self.logger.info(
                    "演练模式：跳过通知发送，正文预览：%s",
                    _preview(verdict, context, self.config.get("APPROVE_COMMAND")),
                )
                return {"status": "would_notify"}
            return {
                "status": "pending_approval",
                "notification": self.notifier.send_drift_verdict(verdict, context),
            }
        raise PipelineError("分级函数返回了无法识别的动作", action=action)

    def _report_failure(self, result):
        """把链路故障发给告警主题；通知本身失败不再改写原始异常。"""
        context = {
            "stage": self.stage,
            "plan_job_id": self.config.get("PLAN_JOB_ID"),
            "apply_job_id": self.config.get("APPLY_JOB_ID"),
            "plan_key": self.plan_key,
            "plan_build_no": (self.plan_build or {}).get("build_no"),
            "apply_build_no": (self.apply_build or {}).get("build_no"),
            "status": (self.plan_build or {}).get("status"),
            "err_msg": (self.plan_build or {}).get("err_msg"),
            "detail": result.get("error"),
        }
        try:
            result["notification"] = self.notifier.send_failure(
                "巡检链路在「%s」阶段失败" % self.stage, context
            )
        except Exception as error:  # 通知失败只可能让排障更难，不能掩盖业务异常
            self.logger.error("故障通知发送异常 detail=%s", error)


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"raw": text}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    return {"raw": value}


def _truncate(payload, limit=500):
    text = json.dumps(payload, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


def _preview(verdict, context, approve_command):
    subject, message = build_drift_message(verdict, context, approve_command)
    return "%s | %s" % (subject, message.replace("\n", " / "))


if __name__ == "__main__":  # 本地手工验证入口：python index.py <事件 JSON>
    import sys

    raw_event = sys.argv[1] if len(sys.argv) > 1 else "{}"
    print(json.dumps(main_handler(json.loads(raw_event), None), ensure_ascii=False, indent=2))
