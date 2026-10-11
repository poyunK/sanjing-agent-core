"""Ark Responses 适配离线测试；只用合成 Key 和 Mock HTTP，不访问真实服务。"""

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from sanjing import datapro, model
from sanjing.config import (ARK_API_URL, ARK_CONTEXT_TOKENS, ARK_MAX_OUTPUT_TOKENS,
                            MAX_OUTPUT_TOKENS)
from sanjing.key_loader import KeyFileError, load_ark_key


class FakeResponse:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body[:size] if size >= 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ArkModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key_file = Path(self.temp.name) / "synthetic-key.yaml"
        self.synthetic_key = "test_ark_token_4Nq7"
        self.key_file.write_text(self.synthetic_key, encoding="utf-8")

    def test_plain_and_mapping_key_files_and_datapro_default(self):
        self.assertEqual(load_ark_key(self.key_file), self.synthetic_key)
        self.key_file.write_text('api_key: "mapped_test_token_123"\n', encoding="utf-8")
        self.assertEqual(load_ark_key(self.key_file), "mapped_test_token_123")
        self.assertEqual(datapro.DEFAULT_KEY_FILE, Path("C:/Users/pla53519/Documents/火山key.yaml"))
        self.assertEqual(datapro._load_key(self.key_file), "mapped_test_token_123")

    def test_datapro_default_follows_ark_key_environment_unless_overridden(self):
        ark_path = Path(self.temp.name) / "configured-ark-key.yaml"
        datapro_path = Path(self.temp.name) / "configured-datapro-key.yaml"
        with patch.dict(os.environ, {"SANJING_ARK_KEY_FILE": str(ark_path)}, clear=True):
            self.assertEqual(datapro._key_path(), ark_path)
            os.environ["SANJING_DATAPRO_KEY_FILE"] = str(datapro_path)
            self.assertEqual(datapro._key_path(), datapro_path)

    def test_key_loader_rejects_ambiguous_or_non_scalar_mapping(self):
        self.key_file.write_text("api_key: token_a\nark_key: token_b\n", encoding="utf-8")
        with self.assertRaises(KeyFileError) as caught:
            load_ark_key(self.key_file)
        self.assertEqual(caught.exception.code, "KEY_INVALID")
        self.key_file.write_text("api_key: !!python/object/apply:os.system [echo bad]\n", encoding="utf-8")
        with self.assertRaises(KeyFileError):
            load_ark_key(self.key_file)

    def test_posts_exact_responses_endpoint_and_converts_tools_and_usage(self):
        response_body = {
            "status": "completed",
            "output": [
                {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": "先查资料"}]},
                {"id": "fc_1", "type": "function_call", "call_id": "call_1",
                 "name": "get_record", "arguments": '{"id":"demo"}', "status": "completed"},
            ],
            "usage": {"input_tokens": 41, "output_tokens": 12, "total_tokens": 53,
                      "input_tokens_details": {"cached_tokens": 8},
                      "output_tokens_details": {"reasoning_tokens": 3}},
        }
        captured = {}

        def fake_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(response_body)

        messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "问题"}]
        tools = [{"type": "function", "function": {
            "name": "get_record", "description": "读取记录",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }}]
        with patch("sanjing.model._request_ark", side_effect=fake_request) as request_mock:
            result = model.ask_llm(messages, tools, provider="ark", key_file=self.key_file)

        request = captured["request"]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://ark.cn-beijing.volces.com/api/plan/v3/responses")
        self.assertEqual(request.full_url, ARK_API_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(captured["timeout"], 60)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.synthetic_key)
        self.assertEqual(body["model"], "ark-code-latest")
        self.assertEqual(body["input"], [{"role": "system", "content": "规则"},
                                         {"role": "user", "content": "问题"}])
        self.assertEqual(body["tools"][0]["name"], "get_record")
        self.assertNotIn("function", body["tools"][0])
        self.assertEqual(body["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(body["store"], False)
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertEqual(request_mock.call_count, 1)
        self.assertEqual(result["message"]["content"], "先查资料")
        self.assertEqual(result["message"]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(result["usage"]["prompt_tokens"], 41)
        self.assertEqual(result["usage"]["completion_tokens"], 12)
        self.assertEqual(result["usage"]["input_tokens_details"], {"cached_tokens": 8})
        self.assertEqual(result["usage"]["output_tokens_details"], {"reasoning_tokens": 3})

    def test_configured_provider_defaults_to_ark_and_limits_are_capabilities_not_request_size(self):
        with patch("sanjing.model.PROVIDER_NAME", "ark"), \
                patch("sanjing.model._read_key", return_value=self.synthetic_key) as read_key, \
                patch("sanjing.model._request_ark", return_value=FakeResponse({
                    "status": "completed", "output": [{"type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text", "text": "ok"}]}],
                })):
            result = model.ask_llm([], [], key_file=self.key_file)
        read_key.assert_called_once_with("ark", self.key_file)
        self.assertEqual(result["message"]["content"], "ok")
        self.assertEqual(ARK_CONTEXT_TOKENS, 256_000)
        self.assertEqual(ARK_MAX_OUTPUT_TOKENS, 32_000)
        self.assertEqual(MAX_OUTPUT_TOKENS, 1500)

    def test_reasoning_and_function_outputs_continue_in_memory_by_call_id(self):
        response = {
            "status": "completed",
            "output": [
                {"id": "rs_1", "type": "reasoning", "encrypted_content": "opaque-cipher",
                 "summary": [{"type": "summary_text", "text": "must not be copied"}]},
                {"id": "fc_2", "type": "function_call", "call_id": "call_same",
                 "name": "lookup", "arguments": "{}", "status": "completed"},
            ],
        }
        normalized = model.parse_responses_response(response)["message"]
        self.assertNotIn("summary", normalized["_responses_output"][0])
        self.assertEqual(set(normalized["_responses_output"][0]), {"id", "type", "encrypted_content"})
        continuation = model._responses_input([
            {"role": "user", "content": "查找"}, normalized,
            {"role": "tool", "tool_call_id": "call_same", "content": '{"ok":true}'},
        ])
        self.assertEqual(continuation[1]["type"], "reasoning")
        self.assertEqual(continuation[1]["encrypted_content"], "opaque-cipher")
        self.assertEqual(continuation[2]["type"], "function_call")
        self.assertEqual(continuation[3], {"type": "function_call_output", "call_id": "call_same",
                                           "output": '{"ok":true}'})
        self.assertFalse(any("previous_response_id" in item for item in continuation))

    def test_responses_status_empty_and_malformed_outputs_fail_closed(self):
        cases = [
            ({"status": "incomplete", "output": []}, "RESPONSE_TRUNCATED"),
            ({"status": "failed", "output": []}, "RESPONSE_FAILED"),
            ({"status": "queued", "output": []}, "BAD_RESPONSE"),
            ({"status": "completed", "output": []}, "EMPTY_RESPONSE"),
        ]
        for payload, code in cases:
            with self.subTest(code=code), self.assertRaises(model.ModelError) as caught:
                model.parse_responses_response(payload)
            self.assertEqual(caught.exception.code, code)

    def test_response_size_limit_and_safe_redirect_rejection(self):
        with patch("sanjing.model._request_ark", return_value=FakeResponse(b"x" * (model.MAX_RESPONSE_BYTES + 1))):
            with self.assertRaises(model.ModelError) as caught:
                model.ask_llm([], [], provider="ark", key_file=self.key_file)
        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

        handler = model._SameOriginRedirectHandler()
        request = urllib.request.Request(ARK_API_URL, data=b"{}", method="POST")
        with self.assertRaises(model._CrossOriginRedirect):
            handler.redirect_request(request, object(), 307, "Temporary Redirect", {},
                                     "https://attacker.example/responses")

        with patch("sanjing.model._request_ark", side_effect=model._CrossOriginRedirect):
            with self.assertRaises(model.ModelError) as caught:
                model.ask_llm([], [], provider="ark", key_file=self.key_file)
        self.assertEqual(caught.exception.code, "CROSS_ORIGIN_REDIRECT")

    def test_http_failure_is_sanitized_and_never_retried(self):
        error = urllib.error.HTTPError(ARK_API_URL, 401, self.synthetic_key, {}, None)
        with patch("sanjing.model._request_ark", side_effect=error) as request_mock:
            with self.assertRaises(model.ModelError) as caught:
                model.ask_llm([], [], provider="ark", key_file=self.key_file)
        self.assertEqual(caught.exception.code, "HTTP_401")
        self.assertNotIn(self.synthetic_key, str(caught.exception))
        self.assertEqual(request_mock.call_count, 1)

    def test_ark_secret_scan_requires_explicit_provider(self):
        candidate = Path(self.temp.name) / "source.py"
        candidate.write_text("safe", encoding="utf-8")
        with patch("sanjing.model._read_key", return_value=self.synthetic_key) as read_key:
            result = model.check_secret_leaks([candidate], key_file=self.key_file)
        read_key.assert_called_once_with("deepseek", self.key_file)
        self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
