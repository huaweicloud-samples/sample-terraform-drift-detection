# -*- coding: utf-8 -*-
"""代码与基础设施声明的一致性校验。

函数只允许读取 ``infra/locals.tf`` 里已经声明的用户数据键。本模块直接解析
基础设施源码做双向比对，避免「代码读了个从未下发的键」或「下发了没人用的键」：

1. 代码声明的必填项 == 基础设施声明的键集合；
2. 代码里出现的取键调用，全部落在对应函数已声明的键集合内
   （公共能力层按两个函数声明键的并集校验）。
"""

import re
import unittest

from tests.support import FUNCTIONS_DIR, REPO_ROOT

from common.config import REQUIRED_KEYS
from common.metrics import (
    METRIC_DRIFT_DETECTED,
    METRIC_NO_DRIFT,
    METRIC_RESOURCE_COUNT,
    SEVERITY_METRICS,
)

LOCALS_FILE = REPO_ROOT / "infra" / "locals.tf"

# 运行期由平台或凭据模块提供的键，不属于用户数据声明范围
RUNTIME_KEYS = {"HW_ACCESS_KEY", "HW_SECRET_KEY", "HW_SECURITY_TOKEN", "HW_PROJECT_ID"}

_BLOCK_PATTERN = r"%s\s*=\s*\{\n(.*?)\n\s*\}"
_KEY_LINE_PATTERN = r"^\s*([A-Z][A-Z0-9_]*)\s*="
_ACCESSOR_PATTERN = re.compile(
    r"\.(?:get|required|require|int_of|float_of)\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
)


def declared_keys(block_name):
    """从基础设施源码里取出某个用户数据块声明的键名集合。"""
    text = LOCALS_FILE.read_text(encoding="utf-8")
    match = re.search(_BLOCK_PATTERN % re.escape(block_name), text, re.S)
    if match is None:
        raise AssertionError("infra/locals.tf 里找不到 %s 块" % block_name)
    keys = set()
    for line in match.group(1).splitlines():
        found = re.match(_KEY_LINE_PATTERN, line)
        if found:
            keys.add(found.group(1))
    return keys


def role_of(path):
    """按所在目录判断源码属于哪个函数；公共层两个函数共用。"""
    parts = path.parts
    if "orchestrator" in parts:
        return "orchestrator"
    if "classifier" in parts:
        return "classifier"
    if "common" in parts:
        return "shared"
    return None


class InfraContractTest(unittest.TestCase):
    def setUp(self):
        self.declared = {
            "orchestrator": declared_keys("orchestrator_user_data"),
            "classifier": declared_keys("classifier_user_data"),
        }
        self.declared["shared"] = self.declared["orchestrator"] | self.declared["classifier"]

    def test_required_keys_match_declared_user_data(self):
        """必填项与基础设施声明逐字一致，既不缺失也不多要。"""
        for role, keys in REQUIRED_KEYS.items():
            self.assertEqual(
                set(keys),
                self.declared[role],
                "%s 的配置键与 infra/locals.tf 声明不一致" % role,
            )

    def test_code_only_reads_declared_keys(self):
        """代码里的取键调用只允许使用已声明的键。"""
        offenders = []
        for path in sorted(FUNCTIONS_DIR.rglob("*.py")):
            if "tests" in path.parts:
                continue
            role = role_of(path)
            if role is None:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                for match in _ACCESSOR_PATTERN.finditer(line):
                    key = match.group(1)
                    if key in RUNTIME_KEYS or key in self.declared[role]:
                        continue
                    offenders.append("%s: %s" % (path.name, key))
        self.assertEqual([], offenders, "代码读取了基础设施未声明的键")

    def test_metric_names_are_used_verbatim(self):
        """指标名与巡检大盘的取数配置同名。"""
        monitoring = (REPO_ROOT / "infra" / "monitoring.tf").read_text(encoding="utf-8")
        names = [
            METRIC_DRIFT_DETECTED,
            METRIC_NO_DRIFT,
            METRIC_RESOURCE_COUNT,
        ] + sorted(SEVERITY_METRICS.values())
        self.assertEqual(6, len(names))
        for name in names:
            self.assertIn(name, monitoring, "%s 未在巡检大盘中声明" % name)

    def test_metric_namespace_has_no_hyphen(self):
        """自定义指标命名空间禁止含连字符，否则上报会被拒收。"""
        variables = (REPO_ROOT / "infra" / "variables.tf").read_text(encoding="utf-8")
        match = re.search(
            r"variable\s+\"ces_metric_namespace\"\s*\{[\s\S]*?default\s*=\s*\"([^\"]+)\"",
            variables,
        )
        self.assertIsNotNone(match, "未声明自定义指标命名空间默认值")
        namespace = match.group(1)
        self.assertNotIn("-", namespace)
        self.assertRegex(namespace, r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")


if __name__ == "__main__":
    unittest.main()
