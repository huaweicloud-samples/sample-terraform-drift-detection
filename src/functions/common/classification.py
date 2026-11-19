"""资源变更的风险分级规则。

分级的业务出发点：一次「把资源拉回代码声明状态」的修复动作，
对不同类型的资源代价差别极大，因此按「变更影响面」分三档，
只有最低档才允许自动修复，其余两档必须人工确认后才动手。

三档口径：

- **critical**：安全边界、访问控制、权限授予、加密配置。这类资源被误改后
  要么立刻放大暴露面，要么直接切断已有访问；自动回滚可能把运维临时放通的
  规则一并冲掉，因此只发通知、等待确认。
- **high**：承载业务的资源本体与巡检链路自身（存储桶、函数、构建任务、
  日志通道、消息主题等）。变更可能引起重启、中断或链路自锁，同样需要确认。
- **low**：只改标签这类不影响行为与连通性的元数据变更，可以自动修复。

未收录的资源类型一律按 **high** 处理（默认从严）。
"""

from common import plan_parser

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_LOW = "low"
SEVERITY_NONE = "none"

ACTION_NONE = "none"
ACTION_AUTO_REMEDIATE = "auto_remediate"
ACTION_NOTIFY_AND_APPROVE = "notify_and_approve"

# 安全边界、访问控制、权限与加密：任何增删改都需要人工确认
CRITICAL_RESOURCE_TYPES = {
    "huaweicloud_networking_secgroup",
    "huaweicloud_networking_secgroup_rule",
    "huaweicloud_obs_bucket_policy",
    "huaweicloud_obs_bucket_bpa",
    "huaweicloud_identity_role",
    "huaweicloud_identity_group_role_assignment",
    "huaweicloud_identity_agency",
    "huaweicloud_kms_key",
    "huaweicloud_kms_grant",
}

# 业务资源本体与巡检链路自身：变更可能引起中断，需要人工确认
HIGH_RISK_RESOURCE_TYPES = {
    "huaweicloud_obs_bucket",
    "huaweicloud_fgs_function",
    "huaweicloud_fgs_trigger",
    "huaweicloud_codearts_build_task",
    "huaweicloud_codearts_project",
    "huaweicloud_identity_group",
    "huaweicloud_lts_group",
    "huaweicloud_lts_stream",
    "huaweicloud_smn_topic",
    "huaweicloud_smn_subscription",
    "huaweicloud_vpc",
    "huaweicloud_vpc_subnet",
    "huaweicloud_compute_instance",
    "huaweicloud_evs_volume",
    "huaweicloud_dcs_instance",
    "huaweicloud_rds_instance",
}

# 判定「仅标签变更」时忽略的属性键：标签是纯元数据，不影响资源行为
METADATA_ONLY_KEYS = frozenset({"tags"})

_SEVERITY_ORDER = (SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_LOW)


def severity_of(change):
    """单条变更的风险等级。"""
    resource_type = str(change.get("type") or "")
    if resource_type in CRITICAL_RESOURCE_TYPES:
        return SEVERITY_CRITICAL
    if resource_type in HIGH_RISK_RESOURCE_TYPES:
        return SEVERITY_HIGH
    if is_metadata_only(change):
        return SEVERITY_LOW
    return SEVERITY_HIGH


def is_metadata_only(change):
    """是否只动了标签。

    计划输出里 ``after`` 会把「尚未确定」的属性写成 ``null``，
    这些空值不代表变更内容，比较前先剔除，避免把纯标签变更误判成属性变更。
    """
    before = _comparable(change.get("before"))
    after = _comparable(change.get("after"))
    if before == after:
        return True
    return _strip_metadata(before) == _strip_metadata(after)


def entry_of(change):
    """变更的对外描述，用于返回值与通知文案。"""
    return {
        "address": change.get("address") or "unknown",
        "type": change.get("type") or "",
        "name": change.get("name") or "",
        "actions": list(change.get("actions") or []),
        "action": change.get("action") or plan_parser.collapse_action(change.get("actions")),
        "destructive": plan_parser.destructive(change),
        "replace_paths": _compact_replace_paths(change.get("replace_paths")),
    }


def classify(changes):
    """对一批变更做分级，返回结论与分组明细。

    :param changes: :class:`common.plan_parser.PlanInsight` 归一化后的变更列表
    :returns: ``{severity, action, message, counts, critical_changes, high_changes, low_changes}``
    """
    groups = {SEVERITY_CRITICAL: [], SEVERITY_HIGH: [], SEVERITY_LOW: []}
    for change in changes or []:
        if not plan_parser.is_actionable(change):
            continue
        groups[severity_of(change)].append(entry_of(change))

    total = sum(len(items) for items in groups.values())
    if total == 0:
        return {
            "severity": SEVERITY_NONE,
            "action": ACTION_NONE,
            "message": "未发现需要处理的配置差异",
            "counts": {"critical": 0, "high": 0, "low": 0, "total": 0},
            "critical_changes": [],
            "high_changes": [],
            "low_changes": [],
        }

    severity = next(name for name in _SEVERITY_ORDER if groups[name])
    action = (
        ACTION_AUTO_REMEDIATE
        if severity == SEVERITY_LOW
        else ACTION_NOTIFY_AND_APPROVE
    )
    message = "共 %d 项差异：严重 %d、高 %d、低 %d" % (
        total,
        len(groups[SEVERITY_CRITICAL]),
        len(groups[SEVERITY_HIGH]),
        len(groups[SEVERITY_LOW]),
    )
    return {
        "severity": severity,
        "action": action,
        "message": message,
        "counts": {
            "critical": len(groups[SEVERITY_CRITICAL]),
            "high": len(groups[SEVERITY_HIGH]),
            "low": len(groups[SEVERITY_LOW]),
            "total": total,
        },
        "critical_changes": groups[SEVERITY_CRITICAL],
        "high_changes": groups[SEVERITY_HIGH],
        "low_changes": groups[SEVERITY_LOW],
    }


def _comparable(value):
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"__value__": value}


def _strip_metadata(mapping):
    if not isinstance(mapping, dict):
        return mapping
    cleaned = {}
    for key, value in mapping.items():
        if key in METADATA_ONLY_KEYS:
            continue
        if value is None:
            continue
        cleaned[key] = value
    return cleaned


def _compact_replace_paths(paths, limit=5):
    if not isinstance(paths, list):
        return []
    flattened = []
    for path in paths[:limit]:
        if isinstance(path, list):
            flattened.append(".".join(str(step) for step in path))
        else:
            flattened.append(str(path))
    return flattened
