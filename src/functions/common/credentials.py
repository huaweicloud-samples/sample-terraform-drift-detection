"""运行凭据获取。

函数绑定委托后，运行期凭据是**临时**的密钥对 + 安全令牌，只经调用上下文签发，
代码里不出现任何长期密钥，也不假定密钥来自哪个变量名以外的来源。

获取顺序：

1. 函数运行上下文（``getSecurityAccessKey`` / ``getSecuritySecretKey`` / ``getSecurityToken``）；
2. 环境变量 ``HW_ACCESS_KEY`` / ``HW_SECRET_KEY`` / ``HW_SECURITY_TOKEN``
   （仅在本地开发与单测里使用，值一律是占位符，不落盘）。

项目级定位信息取 ``HW_PROJECT_ID``；缺失时可从函数 URN 反解
（URN 第 4 段即项目 ID），保证只声明一份来源。
"""

from common.errors import ConfigError

_ENV_ACCESS_KEY = "HW_ACCESS_KEY"
_ENV_SECRET_KEY = "HW_SECRET_KEY"
_ENV_SECURITY_TOKEN = "HW_SECURITY_TOKEN"
_ENV_PROJECT_ID = "HW_PROJECT_ID"


class Credentials(object):
    """一次调用可用的凭据与定位信息。

    属性值只在内存里存在，日志与返回值一律通过 :meth:`redacted` 输出脱敏视图。
    """

    def __init__(self, access_key, secret_key, security_token=None, project_id=None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.security_token = security_token
        self.project_id = project_id

    def redacted(self):
        return {
            "access_key": _mask(self.access_key),
            "secret_key": "<masked>",
            "security_token": "<masked>" if self.security_token else None,
            "project_id": self.project_id,
            "source": getattr(self, "source", None),
        }

    def __repr__(self):
        return "Credentials(%s)" % self.redacted()


def resolve(context=None, environ=None, project_id_hint=None):
    """取出本次调用可用的凭据。

    :param context: 函数运行上下文
    :param environ: 环境变量映射（单测注入假值用）
    :param project_id_hint: 已知的项目 ID（例如从配置或函数 URN 得到）
    :raises ConfigError: 密钥缺失，或项目 ID 无从确定
    """
    import os

    env = dict(environ if environ is not None else os.environ)
    access_key = secret_key = security_token = None
    source = "context"

    if context is not None:
        access_key = _call(context, "getSecurityAccessKey")
        secret_key = _call(context, "getSecuritySecretKey")
        security_token = _call(context, "getSecurityToken")

    if not access_key or not secret_key:
        source = "environment"
        access_key = access_key or env.get(_ENV_ACCESS_KEY)
        secret_key = secret_key or env.get(_ENV_SECRET_KEY)
        security_token = security_token or env.get(_ENV_SECURITY_TOKEN)

    missing = [
        name
        for name, value in (
            ("access key", access_key),
            ("secret key", secret_key),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "未取得运行凭据：请确认函数已绑定委托，或在本地按部署脚本的方式提供占位凭据",
            missing=missing,
        )

    project_id = project_id_hint or env.get(_ENV_PROJECT_ID) or _call(
        context, "getProjectId"
    )
    creds = Credentials(
        access_key=access_key,
        secret_key=secret_key,
        security_token=security_token or None,
        project_id=project_id or None,
    )
    creds.source = source
    return creds


def project_id_from_urn(function_urn):
    """从函数 URN 反解项目 ID。

    URN 形如 ``urn:fss:<region>:<project_id>:function:<group>:<name>:latest``，
    第 4 段是区域项目 ID（不是主账号 ID），编排函数据此拼调用与上报的路径参数。
    """
    if not function_urn:
        return None
    parts = str(function_urn).split(":")
    if len(parts) >= 4 and parts[0] == "urn":
        return parts[3]
    return None


def _call(obj, name):
    getter = getattr(obj, name, None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception:
        return None
    return value if value else None


def _mask(value):
    if not value:
        return None
    text = str(value)
    if len(text) <= 4:
        return "****"
    return text[:2] + "****" + text[-2:]
