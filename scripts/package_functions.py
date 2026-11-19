#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建函数代码包与受管资源代码包。

产出三份压缩包，路径与 ``infra/variables.tf`` 的默认取值逐字一致：

========================================================  =======================
产物                                                      挂载位置
========================================================  =======================
``src/functions/build/orchestrator.zip``                  编排函数的代码包
``src/functions/build/classifier.zip``                    分级函数的代码包
``build/target-infra.zip``                                受管资源源码包
========================================================  =======================

函数代码包的硬性要求（不满足即构建失败，避免上传后才在运行期暴露）：

1. **压缩包根目录必须有 ``index.py``**：函数配置的处理器是 ``index.main_handler``，
   运行时按「处理器首段 = 根目录模块名」加载，多一层目录就会报「找不到入口」。
2. **只允许 ``index.py``、``requirements.txt`` 与 ``common/`` 三个来源**：
   两个函数共用同一份能力层，构建时各拷一份进各自的包。
3. **不打包第三方与原生扩展**：``obs`` 客户端与 ``requests`` 由函数运行时内置，
   服务 SDK 一律不引入（调用走签名请求），原生扩展跨架构后无法加载。
4. **不打包 ``tests``、缓存与字节码**：测试只服务于本地校验，进包会占用下载时间。

受管资源源码包解压到构建工作区根目录，因此包内条目统一带 ``target-infra/`` 前缀，
与 ``buildscripts/plan.sh`` 的 ``TARGET_DIR="${WORKSPACE}/target-infra"`` 对齐。

用法::

    python scripts/package_functions.py            # 三份全建
    python scripts/package_functions.py --only orchestrator classifier
    bash src/functions/build.sh                    # 同一构建入口的薄封装
"""

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS_DIR = REPO_ROOT / "src" / "functions"
COMMON_DIR = FUNCTIONS_DIR / "common"
FUNCTION_BUILD_DIR = FUNCTIONS_DIR / "build"
TARGET_SOURCE_DIR = REPO_ROOT / "target-infra"
TARGET_BUILD_DIR = REPO_ROOT / "build"

ROLES = ("orchestrator", "classifier")

# 固定时间戳：同一份源码重复构建得到字节完全相同的包，便于比对与留档
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# 函数包允许的顶层条目
ALLOWED_TOP_LEVEL = {"index.py", "requirements.txt", "common/"}

# 明确禁止进包的顶层目录/模块：运行时内置或本实践不使用
FORBIDDEN_TOP_LEVEL = {
    "obs",
    "esdk",
    "requests",
    "urllib3",
    "certifi",
    "chardet",
    "idna",
    "setuptools",
    "pip",
    "tests",
    "__pycache__",
}

FORBIDDEN_PREFIXES = ("huaweicloudsdk", "huaweicsdk", "alibabacloud")
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib")

# 受管资源源码包排除的内容：本地状态与插件缓存不属于源码
TARGET_EXCLUDE_NAMES = {".terraform", ".terraform.lock.hcl"}
TARGET_EXCLUDE_SUFFIXES = (".tfstate", ".tfstate.backup", ".tfplan", ".log")


class BuildError(Exception):
    """构建前置校验失败。"""


# ----------------------------------------------------------------------
# 函数代码包
# ----------------------------------------------------------------------
def collect_function_files(role):
    """列出某个函数需要进包的 ``(压缩包内路径, 源文件路径)``。

    顺序确定：先入口与依赖清单，再按路径字典序列出能力层，
    保证同一份源码在任何机器上得到完全相同的包。
    """
    source_dir = FUNCTIONS_DIR / role
    if not source_dir.is_dir():
        raise BuildError("函数源码目录不存在：%s" % source_dir)

    entry = source_dir / "index.py"
    if not entry.is_file():
        raise BuildError("函数入口缺失：%s" % entry)

    pairs = [("index.py", entry)]
    requirements = source_dir / "requirements.txt"
    if requirements.is_file():
        pairs.append(("requirements.txt", requirements))

    if not COMMON_DIR.is_dir():
        raise BuildError("公共能力层目录不存在：%s" % COMMON_DIR)
    for path in sorted(COMMON_DIR.rglob("*")):
        if not path.is_file():
            continue
        if _is_excluded_python_artifact(path):
            continue
        if path.suffix != ".py":
            continue
        relative = path.relative_to(COMMON_DIR).as_posix()
        pairs.append(("common/%s" % relative, path))

    return pairs


def _is_excluded_python_artifact(path):
    if path.name in ("__init__.pyc",):
        return True
    if path.suffix in (".pyc", ".pyo"):
        return True
    return "__pycache__" in path.parts


def verify_python_syntax(pairs):
    """逐个编译源码，语法错误在打包阶段就失败。"""
    for archive_path, source_path in pairs:
        try:
            compile(source_path.read_text(encoding="utf-8"), archive_path, "exec")
        except SyntaxError as error:
            raise BuildError(
                "语法错误：%s（行 %s）%s" % (source_path, error.lineno, error.msg)
            )


def verify_package_entries(pairs):
    """校验包内条目布局与禁止内容。"""
    names = [archive_path for archive_path, _ in pairs]

    if "index.py" not in names:
        raise BuildError("包内根目录缺少 index.py，函数处理器 index.main_handler 无法加载")

    for archive_path in names:
        top = archive_path.split("/", 1)[0]
        if archive_path.count("/") and not archive_path.startswith("common/"):
            raise BuildError("包内出现未预期的层级：%s" % archive_path)
        if not archive_path.startswith("common/") and archive_path not in ALLOWED_TOP_LEVEL:
            raise BuildError("包内出现未预期的条目：%s" % archive_path)
        if top in FORBIDDEN_TOP_LEVEL or top.startswith(FORBIDDEN_PREFIXES):
            raise BuildError(
                "禁止把运行时内置或不使用的模块打进包：%s" % archive_path
            )
        if archive_path.endswith(FORBIDDEN_SUFFIXES):
            raise BuildError("禁止打包原生扩展或字节码：%s" % archive_path)

    # 能力层必须齐备，否则入口在运行期才报导入错误
    required_modules = {
        "common/signer.py",
        "common/cloud_api.py",
        "common/plan_parser.py",
        "common/classification.py",
        "common/metrics.py",
        "common/notify.py",
        "common/obs_store.py",
        "common/runtime.py",
    }
    missing = sorted(required_modules - set(names))
    if missing:
        raise BuildError("能力层缺少模块：%s" % ", ".join(missing))


def build_function_package(role, output):
    """生成某个函数的代码包，返回 ``(路径, 条目数, sha256 前 12 位)``。"""
    pairs = collect_function_files(role)
    verify_python_syntax(pairs)
    verify_package_entries(pairs)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_path, source_path in pairs:
            info = zipfile.ZipInfo(archive_path, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 固定权限位，避免不同平台的文件模式写进包元数据
            info.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(info, source_path.read_bytes())

    return output, len(pairs), _sha256_prefix(output)


# ----------------------------------------------------------------------
# 受管资源源码包
# ----------------------------------------------------------------------
def collect_target_files():
    """列出受管资源源码包需要收录的文件。"""
    if not TARGET_SOURCE_DIR.is_dir():
        raise BuildError("受管资源目录不存在：%s" % TARGET_SOURCE_DIR)

    paths = []
    for path in sorted(TARGET_SOURCE_DIR.rglob("*")):
        if not path.is_file():
            continue
        archive_path = path.relative_to(TARGET_SOURCE_DIR.parent).as_posix()
        name = path.name
        if name in TARGET_EXCLUDE_NAMES:
            continue
        if ".terraform" in path.parts:
            continue
        if name.endswith(TARGET_EXCLUDE_SUFFIXES):
            continue
        paths.append((archive_path, path))

    if not any(archive_path.endswith("/main.tf") for archive_path, _ in paths):
        raise BuildError("受管资源源码包缺少 main.tf")
    return paths


def build_target_package(output):
    """生成受管资源源码包，包内条目统一带 ``target-infra/`` 前缀。"""
    paths = collect_target_files()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_path, source_path in paths:
            info = zipfile.ZipInfo(archive_path, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(info, source_path.read_bytes())
    return output, len(paths), _sha256_prefix(output)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def _sha256_prefix(path, length=12):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:length]


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only",
        nargs="*",
        choices=list(ROLES) + ["target-infra"],
        help="只构建指定产物；缺省构建全部",
    )
    parser.add_argument(
        "--output-dir",
        help="函数代码包输出目录，默认 src/functions/build",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    selected = args.only if args.only else list(ROLES) + ["target-infra"]
    function_output = Path(args.output_dir) if args.output_dir else FUNCTION_BUILD_DIR

    failures = []
    for name in selected:
        try:
            if name == "target-infra":
                path, count, digest = build_target_package(TARGET_BUILD_DIR / "target-infra.zip")
            else:
                path, count, digest = build_function_package(name, function_output / ("%s.zip" % name))
        except BuildError as error:
            failures.append(str(error))
            print("[FAIL] %s: %s" % (name, error))
            continue
        print(
            "[OK] %-12s %s  条目 %d  sha256 %s  大小 %s KB"
            % (name, _relative(path), count, digest, max(1, path.stat().st_size // 1024))
        )

    if failures:
        print("\n构建未完成：%s" % "; ".join(failures))
        return 1
    return 0


def _relative(path):
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    sys.exit(main())
