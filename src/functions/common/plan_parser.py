"""巡检结果解析。

构建脚本产出的是 Terraform 的计划输出，函数侧要能在三种写法下都拿到同一份
「资源变更清单」，因此解析层负责归一化，分级层只面对归一化结果：

1. **计划 JSON**：``terraform show -json plan.tfplan`` 的输出，主体是
   ``resource_changes`` 数组，每条含 ``change.actions`` / ``before`` / ``after``。
2. **精简摘要**：构建脚本在「无差异」分支里直接写出的
   ``{"drift_detected": false, "resource_changes": []}``。
3. **逐行 JSON 流**：``terraform plan -json`` 的输出，一行一个事件对象，
   变更事件在 ``resource_change`` 字段里，动作名在 ``action_name`` 字段里，
   另有 ``change_summary`` 汇总与 ``diagnostic`` 报错。
   构建脚本只留档其中一条事件时（例如只有汇总），同样按本口径解析。

归一化后的每条变更固定为：
``{address, type, name, mode, provider, actions, action, before, after, replace_paths, source}``
其中 ``action`` 是把 ``actions`` 折叠成的单一语义：
``create`` / ``update`` / ``delete`` / ``replace`` / ``import`` / ``move`` / ``none``。
"""

import json

NO_OP_ACTIONS = {"no-op", "none", "read"}
# ``terraform plan -json`` 里逐行事件的对象类型；单独一条也按流解析
_STREAM_EVENT_TYPES = ("resource_change", "change_summary", "apply_summary", "diagnostic", "apply_error")
_STREAM_ACTION_MAP = {
    "no-op": ["no-op"],
    "create": ["create"],
    "update": ["update"],
    "delete": ["delete"],
    "replace": ["create", "delete"],
    "read": ["read"],
    "import": ["import"],
    "move": ["move"],
}


class PlanInsight(object):
    """一次巡检结果的归一化视图。"""

    def __init__(self, format_name, drift_detected, changes, diagnostics, summary=None):
        self.format = format_name
        self.drift_detected = drift_detected
        self.changes = changes
        self.diagnostics = diagnostics
        self.summary = summary or {}

    def actionable_changes(self):
        return [change for change in self.changes if is_actionable(change)]

    def to_dict(self):
        return {
            "plan_format": self.format,
            "drift_detected": self.drift_detected,
            "change_count": len(self.changes),
            "actionable_count": len(self.actionable_changes()),
            "summary": self.summary,
            "diagnostics": self.diagnostics,
        }


def load(raw):
    """把巡检结果原文解析成 :class:`PlanInsight`。

    :param raw: 对象存储里读到的原文，``bytes`` / ``str`` / 已解析的 ``dict`` 都接受
    """
    text = _to_text(raw)
    if text is None:
        return PlanInsight("empty", False, [], [])

    try:
        document = json.loads(text)
    except ValueError:
        document = None

    if isinstance(document, dict):
        if _is_stream_event(document):
            # 构建脚本只留档一条事件（例如仅汇总）时，仍按逐行流口径解析
            return _from_stream(text)
        return _from_document(document)
    if isinstance(document, list):
        return _from_document({"resource_changes": document})

    return _from_stream(text)


# ----------------------------------------------------------------------
# 结构解析
# ----------------------------------------------------------------------
def _from_document(document):
    # 允许外面再套一层，兼容不同构建脚本的留档写法
    for wrapper in ("plan", "drift_result", "result"):
        inner = document.get(wrapper)
        if isinstance(inner, dict):
            document = inner
            break

    raw_changes = document.get("resource_changes")
    if not isinstance(raw_changes, list):
        raw_changes = []

    changes = [normalize(rc, source="plan_json") for rc in raw_changes]

    # 仅刷新态的差异（-refresh-only 计划）也要计入
    drift_entries = document.get("resource_drift")
    if isinstance(drift_entries, list):
        for entry in drift_entries:
            if isinstance(entry, dict):
                changes.append(_from_refresh_entry(entry))

    diagnostics = _collect_diagnostics(document)
    declared = document.get("drift_detected")
    if isinstance(declared, bool):
        drift_detected = declared or any(is_actionable(c) for c in changes)
    else:
        drift_detected = any(is_actionable(c) for c in changes)

    format_name = "plan_json"
    if not raw_changes and not drift_entries:
        format_name = "summary" if "drift_detected" in document else "unknown"

    summary = {}
    for key in ("format_version", "terraform_version", "timestamp", "drift_detected"):
        if key in document:
            summary[key] = document[key]

    return PlanInsight(format_name, drift_detected, changes, diagnostics, summary)


def _from_stream(text):
    """解析 ``terraform plan -json`` 的逐行 JSON 流。"""
    changes = []
    diagnostics = []
    summary = {}
    parsed_any = False

    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        parsed_any = True
        event_type = event.get("type")

        if event_type == "resource_change":
            payload = event.get("resource_change") or {}
            changes.append(_from_stream_change(payload))
        elif event_type == "change_summary":
            summary["change_summary"] = _resource_counts(event.get("change_summary"))
        elif event_type == "apply_summary":
            summary["apply_summary"] = event.get("apply_summary") or {}
        elif event_type in ("diagnostic", "@level"):
            if event_type == "diagnostic":
                diagnostics.append(_describe_diagnostic(event.get("diagnostic") or event))
        elif event_type == "apply_error":
            diagnostics.append(str(event.get("apply_error") or "执行出错"))

    if not parsed_any:
        return PlanInsight("unknown", False, [], ["内容无法解析为 JSON"])

    drift_detected = any(is_actionable(c) for c in changes)
    if not changes:
        drift_detected = _delta_indicates_drift(_summary_delta(summary.get("change_summary"))) or drift_detected

    return PlanInsight("stream_json", drift_detected, changes, diagnostics, summary)


def _is_stream_event(document):
    """是否为逐行 JSON 流里的单条事件对象（而非计划 JSON 文档）。"""
    return str(document.get("type") or "") in _STREAM_EVENT_TYPES


def _resource_counts(value):
    """汇总事件只保留资源级计数。

    动作级计数与资源级计数表达同一件事，留一份即可；没有 ``resource`` 层的
    旧版汇总原样保留，避免丢失信息。
    """
    if isinstance(value, dict) and isinstance(value.get("resource"), dict):
        return {"resource": dict(value["resource"])}
    return value or {}


def _summary_delta(value):
    """从汇总事件里取出用于判定差异的计数字典。"""
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("resource"), dict):
        return value["resource"]
    return value


def _from_stream_change(payload):
    """逐行流里的单条变更 → 归一化变更。"""
    actions = _STREAM_ACTION_MAP.get(
        str(payload.get("action_name") or "").lower(), ["update"]
    )
    address = payload.get("address") or ""
    resource_type = payload.get("type") or payload.get("resource_type") or ""
    name = payload.get("name") or payload.get("resource_name") or ""
    if not address and resource_type and name:
        address = "%s.%s" % (resource_type, name)
    return {
        "address": address or "unknown",
        "type": resource_type,
        "name": name,
        "mode": payload.get("mode") or "managed",
        "provider": payload.get("provider_reference") or payload.get("provider") or "",
        "actions": list(actions),
        "action": collapse_action(actions),
        "before": payload.get("previous_attributes") or payload.get("before"),
        "after": payload.get("new") or payload.get("after"),
        "replace_paths": [],
        "source": "stream_json",
    }


def _from_refresh_entry(entry):
    """``resource_drift`` 条目：真实状态与状态文件的差异。"""
    actions = ["update"]
    address = entry.get("address") or "unknown"
    return {
        "address": address,
        "type": entry.get("type") or _type_from_address(address),
        "name": entry.get("name") or _name_from_address(address),
        "mode": entry.get("mode") or "managed",
        "provider": entry.get("provider_reference", ""),
        "actions": actions,
        "action": "update",
        "before": entry.get("previous"),
        "after": entry.get("new"),
        "replace_paths": [],
        "source": "resource_drift",
    }


def normalize(raw_change, source="plan_json"):
    """把 ``resource_changes`` 的一条记录归一化。"""
    if not isinstance(raw_change, dict):
        return {
            "address": "unknown",
            "type": "",
            "name": "",
            "mode": "managed",
            "provider": "",
            "actions": [],
            "action": "none",
            "before": None,
            "after": None,
            "replace_paths": [],
            "source": source,
        }

    change_block = raw_change.get("change") if isinstance(raw_change.get("change"), dict) else {}
    actions = _as_action_list(change_block.get("actions") or raw_change.get("actions"))
    address = raw_change.get("address") or "unknown"
    resource_type = raw_change.get("type") or _type_from_address(address)
    replace_paths = change_block.get("replace_paths")

    return {
        "address": address,
        "type": resource_type,
        "name": raw_change.get("name") or _name_from_address(address),
        "mode": raw_change.get("mode") or "managed",
        "provider": raw_change.get("provider_name", ""),
        "actions": actions,
        "action": collapse_action(actions),
        "before": change_block.get("before"),
        "after": change_block.get("after"),
        "replace_paths": replace_paths if isinstance(replace_paths, list) else [],
        "source": source,
    }


# ----------------------------------------------------------------------
# 判定
# ----------------------------------------------------------------------
def is_actionable(change):
    """是否为需要分级的真实变更。

    排除：数据源、``no-op``/``read``、以及既非增删改也非资源归属变更的条目。
    """
    if not isinstance(change, dict):
        return False
    if change.get("mode") == "data":
        return False
    actions = [str(a).lower() for a in (change.get("actions") or [])]
    if not actions:
        return False
    if all(a in NO_OP_ACTIONS for a in actions):
        return False
    return True


def collapse_action(actions):
    """把动作列表折叠成单一语义，便于文案与规则复用。"""
    verbs = {str(a).lower() for a in (actions or [])}
    if not verbs:
        return "none"
    if verbs <= NO_OP_ACTIONS:
        return "none"
    if "create" in verbs and "delete" in verbs:
        return "replace"
    if "delete" in verbs:
        return "delete"
    if "create" in verbs:
        return "create"
    if "update" in verbs:
        return "update"
    if "import" in verbs:
        return "import"
    if "move" in verbs:
        return "move"
    return sorted(verbs)[0]


def destructive(change):
    """变更是否会让资源消失（删除或替换），用于通知文案的排序与强调。"""
    return change.get("action") in ("delete", "replace")


def _as_action_list(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return []


def _collect_diagnostics(document):
    """计划 JSON 只解析结构化诊断，自由文本的控制台输出不参与分级。"""
    items = []
    for entry in document.get("diagnostics") or []:
        if isinstance(entry, dict):
            items.append(_describe_diagnostic(entry))
    return [item for item in items if item]


def _describe_diagnostic(entry):
    if not isinstance(entry, dict):
        return str(entry)
    severity = entry.get("severity") or entry.get("@level") or ""
    summary = entry.get("summary") or entry.get("detail") or entry.get("@message") or ""
    if not summary:
        return ""
    return "%s: %s" % (severity, summary) if severity else str(summary)


def _delta_indicates_drift(delta):
    if not isinstance(delta, dict):
        return False
    for key in ("add", "change", "remove", "import", "replace"):
        try:
            if int(delta.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _type_from_address(address):
    """资源类型恒为倒数第二段：``type.name`` 与 ``module.x.type.name`` 同一条规则。"""
    parts = [p for p in str(address).split(".") if p]
    if len(parts) >= 2:
        return parts[-2]
    return ""


def _name_from_address(address):
    parts = [p for p in str(address).split(".") if p]
    if len(parts) >= 2:
        return parts[-1]
    return ""


def _to_text(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return json.dumps(raw)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    if isinstance(raw, str):
        return raw
    return str(raw)
