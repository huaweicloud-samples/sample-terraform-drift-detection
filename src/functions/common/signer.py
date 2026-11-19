"""密钥签名（AK/SK 直接签名请求）。

巡检链路要调用云监控、消息通知、函数工作流与构建服务，函数没有内置的
服务客户端，因此这里实现一套与华为云 API 网关 v3 签名规范一致的最小签名器，
只用标准库，不引入任何第三方签名依赖。

规范要点（与官方 SDK 的签名行为逐条对齐）：

1. 算法名 ``SDK-HMAC-SHA256``；签名密钥就是密钥原文做一次 HMAC，
   没有按日期/区域/服务逐级派生的过程。
2. 待签名串为三段：算法名、``X-Sdk-Date``、规范化请求串的 SHA-256 十六进制值。
3. 规范化请求 = 方法 + 规范 URI + 规范查询串 + 规范请求头 + 参与签名的头 + 请求体摘要。
4. **规范 URI 以 ``/`` 结尾，而实际发出的请求 URL 不带这个尾斜杠**；
   这一点写错会直接 401，路径含 ``:`` 等字符时先解码再按段重新编码。
5. 参与签名的请求头 = 键名不含下划线的所有请求头，转小写后按字典序排列；
   ``host``、``x-sdk-date``、``x-security-token``、``x-project-id`` 参与签名，
   ``X-Cff-Log-Type`` 这类自定义头同样参与，带下划线的头不参与。
6. 请求体摘要：``application/json`` 请求体取 SHA-256；非 JSON 请求体写
   ``X-Sdk-Content-Sha256: UNSIGNED-PAYLOAD`` 并以该值参与签名；空请求体固定用
   空串的 SHA-256。
"""

import hashlib
import hmac
from datetime import datetime
from urllib.parse import quote, unquote

ALGORITHM = "SDK-HMAC-SHA256"
DATE_FORMAT = "%Y%m%dT%H%M%SZ"
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"

HEADER_DATE = "X-Sdk-Date"
HEADER_HOST = "Host"
HEADER_AUTH = "Authorization"
HEADER_CONTENT_HASH = "X-Sdk-Content-Sha256"
HEADER_SECURITY_TOKEN = "X-Security-Token"
HEADER_PROJECT_ID = "X-Project-Id"

_JSON_CONTENT_TYPES = ("application/json", "application/bson")


def url_encode(value):
    """按签名规范做百分号编码，``~`` 之外全部转义。"""
    return quote(str(value), safe="~")


def canonical_uri(path):
    """规范化请求路径。

    先把路径整体解码，再按 ``/`` 拆段逐段重新编码，最后补上尾斜杠。
    实际请求 URL 用未补尾斜杠的原路径，两者不一致是规范的一部分。
    """
    segments = unquote(path or "/").split("/")
    encoded = "/".join(url_encode(segment) for segment in segments)
    if not encoded.endswith("/"):
        encoded += "/"
    return encoded


def canonical_query_string(query_params):
    """规范化查询串：按键名排序后逐条编码，多值按键分组展开。"""
    pairs = []
    for key, value in sorted(list(query_params or [])):
        encoded_key = url_encode(key)
        if isinstance(value, (list, tuple)):
            for item in sorted(str(v) for v in value):
                pairs.append((encoded_key, url_encode(item)))
        elif isinstance(value, bool):
            pairs.append((encoded_key, url_encode(str(value).lower())))
        else:
            pairs.append((encoded_key, url_encode(value)))
    return "&".join("%s=%s" % pair for pair in pairs)


def signed_header_names(headers):
    """参与签名的头：键名不含下划线，转小写并去重后按字典序排列。"""
    names = set()
    for key in headers:
        lowered = str(key).lower()
        if "_" in lowered:
            continue
        names.add(lowered)
    return sorted(names)


def canonical_headers(headers, names):
    lines = ["%s:%s" % (name, headers[name]) for name in names]
    return "\n".join(lines) + "\n"


def payload_hash(headers, body):
    """按规范求请求体摘要，同时决定要不要写摘要头。"""
    content_type = _get_header(headers, "Content-Type") or ""
    if content_type and not content_type.startswith(_JSON_CONTENT_TYPES):
        headers[HEADER_CONTENT_HASH] = UNSIGNED_PAYLOAD

    if not body:
        return EMPTY_PAYLOAD_HASH
    declared = _get_header(headers, HEADER_CONTENT_HASH)
    if declared:
        return declared
    return hashlib.sha256(_as_bytes(body)).hexdigest()


def build_headers(host, path, method, query_params, headers, body, credentials, when=None):
    """补全签名相关请求头并返回 ``(请求头, 实际请求 URL)``。

    :param host: 服务域名，不含协议前缀
    :param path: 资源路径（路径参数已按 URL 规范编码）
    :param method: HTTP 方法
    :param query_params: ``[(key, value), ...]``
    :param headers: 业务请求头，函数内可原地补全签名头
    :param body: 请求体字节串或字符串，可为空
    :param credentials: :class:`common.credentials.Credentials`
    :param when: 固定的 UTC 时间，单测用它构造可复现的签名
    """
    request_headers = {}
    for key, value in (headers or {}).items():
        if value is None:
            continue
        request_headers[key] = _normalize_value(value)

    request_headers[HEADER_HOST] = host
    stamp = when or datetime.utcnow()
    request_headers[HEADER_DATE] = stamp.strftime(DATE_FORMAT)

    if getattr(credentials, "security_token", None):
        request_headers[HEADER_SECURITY_TOKEN] = _normalize_value(credentials.security_token)
    # 不发送 X-Project-Id：项目 ID 只作为 URL 路径参数使用，不能作为请求头参与签名。
    # 实测带上该头时，构建服务的触发接口返回
    # {"code":"DEV.CB.032500","reason":"pkiToken无效"}，去掉后同一请求返回 200。
    # （其余服务对该头容忍，但构建服务不容忍，故统一不发送。）

    body_bytes = _as_bytes(body) if body else b""
    digest = payload_hash(request_headers, body_bytes if body else None)

    names = signed_header_names(request_headers)
    lowered = {}
    for key, value in request_headers.items():
        lowered[key.lower()] = value
    canonical = canonical_headers(lowered, names)

    canonical_query = canonical_query_string(query_params)
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri(path),
            canonical_query,
            canonical,
            ";".join(names),
            digest,
        ]
    )
    string_to_sign = "%s\n%s\n%s" % (
        ALGORITHM,
        request_headers[HEADER_DATE],
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    )
    signature = hmac.new(
        _as_bytes(credentials.secret_key), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    request_headers[HEADER_AUTH] = "%s Access=%s, SignedHeaders=%s, Signature=%s" % (
        ALGORITHM,
        credentials.access_key,
        ";".join(names),
        signature,
    )

    url = "https://%s%s" % (host, path)
    if canonical_query:
        url = "%s?%s" % (url, canonical_query)
    return request_headers, url


def _get_header(headers, name):
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return value
    return None


def _as_bytes(value):
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _normalize_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value).strip()
