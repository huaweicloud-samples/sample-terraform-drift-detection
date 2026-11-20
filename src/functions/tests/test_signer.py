# -*- coding: utf-8 -*-
"""签名规范化校验。

签名一旦写错，云服务返回的是 401 或 404，排障链路很长，因此这里把规范要点
逐条钉死：规范 URI 的尾斜杠、参与签名的请求头集合、查询串排序、
请求体摘要取值，以及给定输入下的签名基准值。
"""

import hashlib
import hmac
import unittest
from datetime import datetime

from tests import support

from common import signer
from common.credentials import Credentials

CREDS = Credentials(
    access_key="AKEXAMPLEKEYPLACEHOLDER",
    secret_key="skexamplesecretkeyplaceholder",
    security_token="example-security-token",
    project_id="example-project-id",
)
WHEN = datetime(2026, 1, 4, 5, 6, 7)
HOST = "ces.cn-north-4.myhuaweicloud.com"


class CanonicalUriTest(unittest.TestCase):
    def test_root_path(self):
        self.assertEqual("/", signer.canonical_uri("/"))
        self.assertEqual("/", signer.canonical_uri(""))

    def test_trailing_slash_is_added(self):
        self.assertEqual("/V1.0/example-project-id/metric-data/",
                         signer.canonical_uri("/V1.0/example-project-id/metric-data"))

    def test_percent_encoded_segments_are_reencoded(self):
        path = "/v2/p/fgs/functions/%s/invocations" % signer.url_encode(
            "urn:fss:cn-north-4:p:function:default:f:latest"
        )
        canonical = signer.url_encode("urn:fss:cn-north-4:p:function:default:f:latest")
        self.assertEqual("/v2/p/fgs/functions/%s/invocations/" % canonical, signer.canonical_uri(path))

    def test_tilde_is_not_escaped(self):
        self.assertEqual("~", signer.url_encode("~"))
        self.assertEqual("%3A", signer.url_encode(":"))
        self.assertEqual("%20", signer.url_encode(" "))


class CanonicalQueryTest(unittest.TestCase):
    def test_pairs_are_sorted_and_encoded(self):
        query = signer.canonical_query_string([("limit", "10"), ("Action", "Get"), ("name", "a b")])
        self.assertEqual("Action=Get&limit=10&name=a%20b", query)

    def test_empty_query(self):
        self.assertEqual("", signer.canonical_query_string([]))
        self.assertEqual("", signer.canonical_query_string(None))

    def test_bool_value_is_lowercased(self):
        self.assertEqual("flag=true", signer.canonical_query_string([("flag", True)]))

    def test_multi_value_expands(self):
        self.assertEqual(
            "id=1&id=2",
            signer.canonical_query_string([("id", [2, 1])]),
        )


class SignedHeaderSetTest(unittest.TestCase):
    def test_underscore_headers_are_excluded(self):
        names = signer.signed_header_names(
            {"Host": "h", "X-Sdk-Date": "d", "X_Custom": "v", "Content-Type": "application/json"}
        )
        self.assertEqual(["content-type", "host", "x-sdk-date"], names)

    def test_dated_headers_are_included(self):
        names = signer.signed_header_names(
            {"X-Security-Token": "t", "X-Project-Id": "p", "X-Cff-Request-Version": "v1"}
        )
        self.assertEqual(["x-cff-request-version", "x-project-id", "x-security-token"], names)


class PayloadHashTest(unittest.TestCase):
    def test_empty_body_hash(self):
        headers = {"Content-Type": "application/json"}
        self.assertEqual(signer.EMPTY_PAYLOAD_HASH, signer.payload_hash(headers, None))
        self.assertEqual("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                         signer.EMPTY_PAYLOAD_HASH)
        self.assertNotIn("X-Sdk-Content-Sha256", headers)

    def test_json_body_is_hashed(self):
        headers = {"Content-Type": "application/json"}
        body = b'{"a":1}'
        self.assertEqual(hashlib.sha256(body).hexdigest(), signer.payload_hash(headers, body))
        self.assertNotIn("X-Sdk-Content-Sha256", headers)

    def test_non_json_body_declares_unsigned_payload(self):
        headers = {"Content-Type": "text/plain"}
        self.assertEqual(signer.UNSIGNED_PAYLOAD, signer.payload_hash(headers, b"hello"))
        self.assertEqual(signer.UNSIGNED_PAYLOAD, headers["X-Sdk-Content-Sha256"])


class BuildHeadersTest(unittest.TestCase):
    def _signed(self, method="POST", path="/V1.0/example-project-id/metric-data", query=None,
               body=b'{"metric":1}', headers=None):
        return signer.build_headers(
            host=HOST,
            path=path,
            method=method,
            query_params=query or [],
            headers=dict(headers or {"Content-Type": "application/json", "Accept": "application/json"}),
            body=body,
            credentials=CREDS,
            when=WHEN,
        )

    def test_required_headers_are_present(self):
        headers, url = self._signed()
        self.assertEqual("20260104T050607Z", headers["X-Sdk-Date"])
        self.assertEqual(HOST, headers["Host"])
        self.assertEqual("example-security-token", headers["X-Security-Token"])
        self.assertNotIn("X-Project-Id", headers)  # 构建服务不接受该头，见 signer 注释
        self.assertTrue(headers["Authorization"].startswith("SDK-HMAC-SHA256 Access=AKEXAMPLEKEYPLACEHOLDER, "))
        self.assertEqual("https://%s%s" % (HOST, path_of(self._signed()[1])), url)

    def test_wire_url_keeps_no_trailing_slash(self):
        _, url = self._signed()
        self.assertTrue(url.endswith("/metric-data"))
        self.assertNotIn("/metric-data/", url)

    def test_signed_header_list(self):
        headers, _ = self._signed(query=[("qualifier", "latest")])
        signed = headers["Authorization"].split("SignedHeaders=")[1].split(",")[0]
        self.assertEqual("accept;content-type;host;x-sdk-date;x-security-token", signed)

    def test_signature_is_deterministic(self):
        """同一输入必须得到同一签名；签名基准值锁定实现不被误改。"""
        headers, _ = self._signed()
        self.assertEqual(
            "SDK-HMAC-SHA256 Access=AKEXAMPLEKEYPLACEHOLDER, "
            "SignedHeaders=accept;content-type;host;x-sdk-date;x-security-token, "
            "Signature=0a63990d9831e69f04819fd109d13ac49ebc8e901001cf98916cb7cdc7b2c0fc",
            headers["Authorization"],
        )

    def test_signature_matches_manual_derivation(self):
        """按规范手工重算一遍，验证实现与规范一致。"""
        headers, _ = self._signed(query=[("b", "2"), ("a", "1")])
        canonical_request = "\n".join(
            [
                "POST",
                "/V1.0/example-project-id/metric-data/",
                "a=1&b=2",
                "accept:application/json\n"
                "content-type:application/json\n"
                "host:ces.cn-north-4.myhuaweicloud.com\n"
                ""
                "x-sdk-date:20260104T050607Z\n"
                "x-security-token:example-security-token\n",
                "accept;content-type;host;x-sdk-date;x-security-token",
                hashlib.sha256(b'{"metric":1}').hexdigest(),
            ]
        )
        string_to_sign = "SDK-HMAC-SHA256\n20260104T050607Z\n%s" % hashlib.sha256(
            canonical_request.encode("utf-8")
        ).hexdigest()
        expected = hmac.new(
            CREDS.secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        self.assertIn("Signature=%s" % expected, headers["Authorization"])

    def test_get_without_body_uses_empty_hash(self):
        headers, _ = self._signed(method="GET", body=None, headers={"Accept": "application/json"})
        signed = headers["Authorization"].split("Signature=")[1]
        canonical_request = "\n".join(
            [
                "GET",
                "/V1.0/example-project-id/metric-data/",
                "",
                "accept:application/json\nhost:ces.cn-north-4.myhuaweicloud.com\n"
                "x-sdk-date:20260104T050607Z\n"
                "x-security-token:example-security-token\n",
                "accept;host;x-sdk-date;x-security-token",
                signer.EMPTY_PAYLOAD_HASH,
            ]
        )
        string_to_sign = "SDK-HMAC-SHA256\n20260104T050607Z\n%s" % hashlib.sha256(
            canonical_request.encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            hmac.new(CREDS.secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest(),
            signed,
        )

    def test_credentials_without_token_or_project_omit_their_headers(self):
        headers, _ = signer.build_headers(
            host=HOST,
            path="/v3/jobs/build",
            method="POST",
            query_params=[],
            headers={"Content-Type": "application/json"},
            body=b'{"job_id":"job"}',
            credentials=Credentials(access_key=CREDS.access_key, secret_key=CREDS.secret_key),
            when=WHEN,
        )
        self.assertNotIn("X-Security-Token", headers)
        self.assertNotIn("X-Project-Id", headers)
        self.assertEqual("content-type;host;x-sdk-date",
                         headers["Authorization"].split("SignedHeaders=")[1].split(",")[0])


def path_of(url):
    """取 URL 的路径部分，便于断言请求地址形态。"""
    without_scheme = url.split("://", 1)[1]
    return "/" + without_scheme.split("/", 1)[1].split("?", 1)[0]


if __name__ == "__main__":
    unittest.main()
