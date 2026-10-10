"""DataPro MCP 客户端协议与密钥边界测试；所有凭证均为测试假值。"""

import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from sanjing import datapro


class FakeResponse:
    def __init__(self, body=b"", *, content_type="application/json", headers=None, status=200):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.read_sizes = []
        self.offset = 0
        self.closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        result = self.body[self.offset:self.offset + size] if size >= 0 else self.body[self.offset:]
        self.offset += len(result)
        return result

    def readline(self, size=-1):
        if self.offset >= len(self.body):
            return b""
        end = self.body.find(b"\n", self.offset)
        end = len(self.body) if end < 0 else end + 1
        if size >= 0:
            end = min(end, self.offset + size)
        result = self.body[self.offset:end]
        self.offset = end
        return result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False


class OpenEndedSSE(FakeResponse):
    """Delivers the matching event, then fails if the client waits for EOF."""

    def __init__(self, event_body):
        super().__init__(b"", content_type="text/event-stream")
        self.lines = event_body.encode("utf-8").splitlines(keepends=True)
        self.extra_read_attempted = False

    def read(self, size=-1):
        raise AssertionError("SSE client must not read the full response body")

    def readline(self, size=-1):
        if self.lines:
            line = self.lines.pop(0)
            if size >= 0 and len(line) > size:
                self.lines.insert(0, line[size:])
                return line[:size]
            return line
        self.extra_read_attempted = True
        raise AssertionError("SSE client waited for connection EOF after the matching event")


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def rpc_response(request_id, result, *, headers=None):
    return FakeResponse(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
                        headers=headers)


def standard_responses(payload, *, call_response=None):
    return [
        rpc_response(1, {"protocolVersion": datapro.PROTOCOL_VERSION,
                         "capabilities": {}, "serverInfo": {"name": "mock", "version": "1"}},
                     headers={"Mcp-Session-Id": "session-test"}),
        FakeResponse(b"", content_type="application/json", status=202),
        rpc_response(2, {"tools": [{"name": "dataPro_search", "inputSchema": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        }}]}),
        call_response or rpc_response(3, {"structuredContent": payload}),
    ]


class DataProTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key_file = Path(self.temp.name) / "fake-key.txt"
        self.fake_key = "test_fake_datapro_secret_9x7Q"
        self.key_file.write_text(self.fake_key, encoding="utf-8")

    def call_with(self, responses, query="测试查询", timeout_seconds=1):
        opener = FakeOpener(responses)
        with patch("sanjing.datapro._opener", return_value=opener):
            result = datapro.query_data(query, timeout_seconds=timeout_seconds, key_file=self.key_file)
        return result, opener

    def test_json_flow_uses_only_fixed_tool_and_session_headers(self):
        payload = {"code": 0, "msg": "ok", "trace_id": "trace-1", "items": [{"name": "示例"}]}
        result, opener = self.call_with(standard_responses(payload))
        self.assertEqual(result, payload)
        self.assertEqual(len(opener.requests), 4)
        expected_methods = ["initialize", "notifications/initialized", "tools/list", "tools/call"]
        for (request, timeout), method in zip(opener.requests, expected_methods):
            self.assertEqual(request.full_url, datapro.DATAPRO_URL)
            self.assertEqual(request.get_header("Mcp-protocol-version"), datapro.PROTOCOL_VERSION)
            self.assertEqual(request.get_header("X-agent-plan-key"), self.fake_key)
            self.assertGreater(timeout, 0)
            message = json.loads(request.data.decode("utf-8"))
            self.assertEqual(message["method"], method)
        self.assertIsNone(opener.requests[0][0].get_header("Mcp-session-id"))
        for request, _ in opener.requests[1:]:
            self.assertEqual(request.get_header("Mcp-session-id"), "session-test")
        call = json.loads(opener.requests[3][0].data.decode("utf-8"))
        self.assertEqual(call["params"], {"name": "dataPro_search", "arguments": {"query": "测试查询"}})

    def test_sse_skips_notification_and_joins_multiline_data_until_matching_id(self):
        body = (
            'event: message\n'
            'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n'
            'event: message\n'
            'data: {"jsonrpc":"2.0","id":3,"result":{"structuredContent":{"code":0,\n'
            'data: "msg":"done","items":[]}}}\n\n'
        )
        call_sse = FakeResponse(body, content_type="text/event-stream; charset=utf-8")
        call_sse.headers = {"content-type": "text/event-stream; charset=utf-8"}
        result, _ = self.call_with(standard_responses({}, call_response=call_sse))
        self.assertEqual(result, {"code": 0, "msg": "done", "items": []})

    def test_sse_returns_on_matching_event_without_waiting_for_eof(self):
        event = ('data: {"jsonrpc":"2.0","id":3,"result":{"structuredContent":'
                 '{"code":0,"items":[]}}}\n\n')
        response = OpenEndedSSE(event)
        result, _ = self.call_with(standard_responses({}, call_response=response))
        self.assertEqual(result, {"code": 0, "items": []})
        self.assertFalse(response.extra_read_attempted)
        self.assertTrue(response.closed)

    def test_text_payload_fallback_and_exact_secret_redaction(self):
        business = {"code": 0, "note": "echo " + self.fake_key}
        tool_result = {"content": [{"type": "text", "text": json.dumps(business)}]}
        call_response = rpc_response(3, tool_result)
        result, _ = self.call_with(standard_responses({}, call_response=call_response))
        self.assertEqual(result, {"code": 0, "note": "echo [密钥已隐藏]"})
        self.assertNotIn(self.fake_key, json.dumps(result, ensure_ascii=False))

    def test_structured_payload_is_redacted_recursively(self):
        result, _ = self.call_with(standard_responses({"items": [{"text": self.fake_key}]}))
        self.assertEqual(result, {"items": [{"text": "[密钥已隐藏]"}]})

    def test_unknown_tool_or_schema_fails_before_call(self):
        responses = standard_responses({"code": 0})
        responses[2] = rpc_response(2, {"tools": [{"name": "other", "inputSchema": {}}]})
        opener = FakeOpener(responses)
        with patch("sanjing.datapro._opener", return_value=opener):
            with self.assertRaises(datapro.DataproError) as caught:
                datapro.query_data("q", key_file=self.key_file)
        self.assertEqual(caught.exception.code, "TOOL_SCHEMA_MISMATCH")
        self.assertEqual(len(opener.requests), 3)

    def test_protocol_mismatch_is_rejected(self):
        responses = standard_responses({"code": 0})
        responses[0] = rpc_response(1, {"protocolVersion": "2024-11-05"})
        opener = FakeOpener(responses)
        with patch("sanjing.datapro._opener", return_value=opener):
            with self.assertRaises(datapro.DataproError) as caught:
                datapro.query_data("q", key_file=self.key_file)
        self.assertEqual(caught.exception.code, "PROTOCOL_MISMATCH")
        self.assertEqual(len(opener.requests), 1)

    def test_tool_error_and_malformed_business_payload_are_safe_errors(self):
        tool_error = rpc_response(3, {"isError": True, "content": [{"type": "text", "text": "private"}]})
        with self.assertRaises(datapro.DataproError) as caught:
            self.call_with(standard_responses({}, call_response=tool_error))
        self.assertEqual(caught.exception.code, "TOOL_CALL_ERROR")
        self.assertEqual(str(caught.exception), "TOOL_CALL_ERROR")

        malformed = rpc_response(3, {"content": [{"type": "text", "text": "not-json"}]})
        with self.assertRaises(datapro.DataproError) as caught:
            self.call_with(standard_responses({}, call_response=malformed))
        self.assertEqual(caught.exception.code, "INVALID_BUSINESS_PAYLOAD")

    def test_each_response_is_limited_to_one_mib(self):
        oversized = FakeResponse(b" " * (datapro.MAX_RESPONSE_BYTES + 1))
        opener = FakeOpener([oversized])
        with patch("sanjing.datapro._opener", return_value=opener):
            with self.assertRaises(datapro.DataproError) as caught:
                datapro.query_data("q", key_file=self.key_file)
        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")
        self.assertEqual(oversized.read_sizes, [datapro.MAX_RESPONSE_BYTES + 1])

    def test_timeout_is_total_and_late_worker_does_not_return(self):
        opener = FakeOpener([])
        def slow_open(*_args, **_kwargs):
            time.sleep(0.12)
            return FakeResponse(b"{}")
        opener.open = slow_open
        with patch("sanjing.datapro._opener", return_value=opener):
            started = time.monotonic()
            with self.assertRaises(datapro.DataproError) as caught:
                datapro.query_data("q", timeout_seconds=0.02, key_file=self.key_file)
            self.assertLess(time.monotonic() - started, 0.09)
        self.assertEqual(caught.exception.code, "TIMEOUT")
        time.sleep(0.14)  # 等待模拟阻塞请求结束，避免遗留测试线程。

    def test_http_failure_is_not_retried_or_exposed(self):
        error = urllib.error.HTTPError(datapro.DATAPRO_URL, 401, "private response", {}, None)
        opener = FakeOpener([error])
        with patch("sanjing.datapro._opener", return_value=opener):
            with self.assertRaises(datapro.DataproError) as caught:
                datapro.query_data("q", key_file=self.key_file)
        self.assertEqual(caught.exception.code, "HTTP_401")
        self.assertEqual(str(caught.exception), "HTTP_401")
        self.assertEqual(len(opener.requests), 1)

    def test_cross_origin_redirect_is_rejected(self):
        handler = datapro._SameOriginRedirectHandler()
        request = __import__("urllib.request", fromlist=["Request"]).Request(datapro.DATAPRO_URL)
        with self.assertRaises(datapro._CrossOriginRedirect):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/steal")

    def test_key_scanner_returns_only_occurrence_count(self):
        first = Path(self.temp.name) / "first.txt"
        second = Path(self.temp.name) / "second.txt"
        first.write_text("prefix " + self.fake_key + " and " + self.fake_key, encoding="utf-8")
        second.write_text("clean", encoding="utf-8")
        count = datapro.scan_key_leaks([first, second, self.key_file], key_file=self.key_file)
        self.assertEqual(count, 2)
        self.assertIsInstance(count, int)

    def test_invalid_query_and_timeout_are_rejected(self):
        with self.assertRaises(datapro.DataproError) as caught:
            datapro.query_data(" ", key_file=self.key_file)
        self.assertEqual(caught.exception.code, "INVALID_QUERY")
        with self.assertRaises(datapro.DataproError) as caught:
            datapro.query_data("q", timeout_seconds=61, key_file=self.key_file)
        self.assertEqual(caught.exception.code, "INVALID_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
