"""verification.secret_check 的离线测试：替换扫描器，不读真实 Key、不联网。"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sanjing import datapro
from verification import secret_check


def _temp_paths(temp, names):
    """在临时目录里造待扫描文件。"""
    root = Path(temp)
    result = []
    for name in names:
        path = root / name
        path.write_text("", encoding="utf-8")
        result.append(path)
    return result


class SecretCheckOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project_dir = Path(self.temp.name)
        self.paths = _temp_paths(self.temp.name, ["a.txt", "b.md", "c.json"])
        self.report_path = (self.project_dir / "runtime" / "professional_verification"
                            / "secret_report.json")

    def _run_main(self, deepseek, datapro_scan):
        """替换扫描器并把工程根指向临时目录，返回退出码、输出和报告。"""
        buffer = io.StringIO()
        with patch.object(secret_check, "PROJECT_DIR", self.project_dir), \
             patch.object(secret_check.model, "check_secret_leaks", side_effect=deepseek), \
             patch.object(secret_check.datapro, "scan_key_leaks", side_effect=datapro_scan), \
             redirect_stdout(buffer), redirect_stderr(buffer):
            code = secret_check.main([])
        self.assertTrue(self.report_path.is_file())
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        return code, buffer.getvalue(), report

    def _valid_deepseek(self, paths, **extra):
        return {
            "verified": True,
            "scanned_files": len(paths),
            "matching_files": 0,
            **extra,
        }

    def test_both_services_success_no_leak(self):
        code, out, report = self._run_main(self._valid_deepseek, lambda p: 0)
        self.assertEqual(code, 0)
        self.assertEqual(report["deepseek_status"], "verified")
        self.assertEqual(report["datapro_status"], "verified")
        self.assertTrue(report["scan_completed"])
        self.assertEqual(report["summary_codes"], [])
        self.assertEqual(report["datapro_exact_key_occurrences"], 0)
        self.assertEqual(report["deepseek"], {
            "verified": True,
            "scanned_files": len(self.paths),
            "matching_files": 0,
        })
        self.assertTrue(str(self.report_path).startswith(self.temp.name))

    def test_datapro_key_unavailable_still_scans_deepseek(self):
        calls = {"deepseek": 0}

        def fake_deepseek(paths):
            calls["deepseek"] += 1
            return self._valid_deepseek(paths)

        def fake_datapro(paths):
            raise datapro.DataproError("KEY_UNAVAILABLE")

        code, out, report = self._run_main(fake_deepseek, fake_datapro)
        self.assertEqual(code, 1)
        self.assertEqual(calls["deepseek"], 1)
        self.assertEqual(report["datapro_status"], "error")
        self.assertEqual(report["datapro_error_code"], "DATAPRO_KEY_UNAVAILABLE")
        self.assertIsNone(report["datapro_exact_key_occurrences"])
        self.assertEqual(report["deepseek_status"], "verified")
        self.assertIn("VERIFICATION_INCOMPLETE", report["summary_codes"])

    def test_deepseek_failure_still_runs_datapro(self):
        calls = {"datapro": 0}

        def fake_deepseek(paths):
            raise RuntimeError("合成 deepseek 故障")

        def fake_datapro(paths):
            calls["datapro"] += 1
            return 0

        code, out, report = self._run_main(fake_deepseek, fake_datapro)
        self.assertEqual(code, 1)
        self.assertEqual(calls["datapro"], 1)
        self.assertEqual(report["deepseek_status"], "error")
        self.assertEqual(report["deepseek_error_code"], "DEEPSEEK_SCAN_FAILED")
        self.assertIsNone(report["deepseek"])
        self.assertEqual(report["datapro_status"], "verified")
        self.assertEqual(report["datapro_exact_key_occurrences"], 0)

    def test_datapro_exception_maps_to_fixed_error_code(self):
        def fake_datapro(paths):
            raise datapro.DataproError("SCAN_FAILED")

        code, out, report = self._run_main(self._valid_deepseek, fake_datapro)
        self.assertEqual(code, 1)
        self.assertEqual(report["datapro_error_code"], "DATAPRO_SCAN_FAILED")
        self.assertEqual(report["datapro_status"], "error")

    def test_leak_found_is_completed_but_not_passed(self):
        code, out, report = self._run_main(
            lambda p: {"verified": True, "scanned_files": len(p), "matching_files": 2},
            lambda p: 3,
        )
        self.assertEqual(code, 1)
        self.assertTrue(report["scan_completed"])
        self.assertIn("SECRET_LEAK_FOUND", report["summary_codes"])
        self.assertEqual(report["datapro_exact_key_occurrences"], 3)

    def test_leak_only_in_datapro(self):
        code, out, report = self._run_main(self._valid_deepseek, lambda p: 1)
        self.assertEqual(code, 1)
        self.assertIn("SECRET_LEAK_FOUND", report["summary_codes"])
        self.assertTrue(report["scan_completed"])

    def test_exception_with_sensitive_marker_is_redacted(self):
        fake_secret = "sk-FAKE_TEST_SECRET_1234567890"

        def fake_datapro(paths):
            raise datapro.DataproError("SCAN_FAILED: " + fake_secret)

        code, out, report = self._run_main(self._valid_deepseek, fake_datapro)
        self.assertEqual(code, 1)
        combined = out + json.dumps(report, ensure_ascii=False)
        self.assertNotIn(fake_secret, combined)
        self.assertNotIn("SCAN_FAILED:", combined)
        self.assertIn("DATAPRO_SCAN_FAILED", combined)

    def test_deepseek_unverified_result_is_incomplete(self):
        code, out, report = self._run_main(
            lambda p: {"verified": False, "reason": "key_unavailable"},
            lambda p: 0,
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["deepseek_status"], "error")
        self.assertEqual(report["deepseek_error_code"], "DEEPSEEK_SCAN_FAILED")
        self.assertIn("VERIFICATION_INCOMPLETE", report["summary_codes"])
        self.assertEqual(report["deepseek"], {"verified": False})

    def test_deepseek_missing_required_counts_is_incomplete(self):
        malformed_results = [
            {"verified": True, "scanned_files": len(self.paths)},
            {"verified": True, "matching_files": 0},
        ]
        for result in malformed_results:
            with self.subTest(result=result):
                code, out, report = self._run_main(lambda p, r=result: r, lambda p: 0)
                self.assertEqual(code, 1)
                self.assertEqual(report["deepseek_status"], "error")
                self.assertFalse(report["scan_completed"])
                self.assertIn("VERIFICATION_INCOMPLETE", report["summary_codes"])

    def test_deepseek_count_fields_reject_invalid_types_and_ranges(self):
        invalid_values = [None, "0", -1, True, False]
        for field in ("scanned_files", "matching_files"):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    def fake_deepseek(paths, key=field, invalid=value):
                        result = self._valid_deepseek(paths)
                        result[key] = invalid
                        return result

                    code, out, report = self._run_main(fake_deepseek, lambda p: 0)
                    self.assertEqual(code, 1)
                    self.assertEqual(report["deepseek_status"], "error")
                    self.assertFalse(report["scan_completed"])
                    self.assertIn("VERIFICATION_INCOMPLETE", report["summary_codes"])
                    # 非法值本身不写进白名单报告字段。
                    self.assertNotIn(field, report["deepseek"] or {})

    def test_deepseek_verified_requires_true_and_counts_must_be_consistent(self):
        malformed_results = [
            {"verified": 1, "scanned_files": len(self.paths), "matching_files": 0},
            {"verified": True, "scanned_files": 9999, "matching_files": 0},
            {"verified": True, "scanned_files": 1, "matching_files": 2},
        ]
        for result in malformed_results:
            with self.subTest(result=result):
                code, out, report = self._run_main(lambda p, r=result: r, lambda p: 0)
                self.assertEqual(code, 1)
                self.assertEqual(report["deepseek_status"], "error")
                self.assertFalse(report["scan_completed"])

    def test_datapro_count_rejects_none_string_negative_and_bool(self):
        for value in (None, "0", -1, True, False):
            with self.subTest(value=value):
                code, out, report = self._run_main(self._valid_deepseek, lambda p, v=value: v)
                self.assertEqual(code, 1)
                self.assertEqual(report["datapro_status"], "error")
                self.assertEqual(report["datapro_error_code"], "DATAPRO_SCAN_FAILED")
                self.assertIsNone(report["datapro_exact_key_occurrences"])
                self.assertFalse(report["scan_completed"])
                self.assertIn("VERIFICATION_INCOMPLETE", report["summary_codes"])

    def test_malformed_deepseek_still_attempts_datapro(self):
        calls = {"datapro": 0}

        def fake_datapro(paths):
            calls["datapro"] += 1
            return 0

        code, out, report = self._run_main(lambda p: "not a result object", fake_datapro)
        self.assertEqual(code, 1)
        self.assertEqual(calls["datapro"], 1)
        self.assertEqual(report["deepseek_status"], "error")
        self.assertEqual(report["datapro_status"], "verified")
        self.assertFalse(report["scan_completed"])
        self.assertIn("VERIFICATION_INCOMPLETE", report["summary_codes"])

    def test_extra_scanner_fields_are_not_written_to_report_or_terminal(self):
        fake_secret = "sk-FAKE_RESULT_SECRET_9876543210"

        def fake_deepseek(paths):
            return self._valid_deepseek(paths, token=fake_secret, private_detail={"token": fake_secret})

        code, out, report = self._run_main(fake_deepseek, lambda p: 0)
        self.assertEqual(code, 0)
        self.assertEqual(set(report["deepseek"]), {"verified", "scanned_files", "matching_files"})
        self.assertNotIn(fake_secret, out + json.dumps(report, ensure_ascii=False))

    def test_report_structure_on_success(self):
        code, out, report = self._run_main(self._valid_deepseek, lambda p: 0)
        self.assertEqual(code, 0)
        self.assertEqual(report["files_scanned"], len(self.paths))
        self.assertEqual(report["deepseek"]["matching_files"], 0)
        self.assertEqual(report["datapro_exact_key_occurrences"], 0)
        self.assertEqual(json.loads(self.report_path.read_text(encoding="utf-8")), report)


if __name__ == "__main__":
    unittest.main()
