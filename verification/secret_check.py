"""本机交付前扫描：密钥仍只由各请求模块读取，报告不包含密钥和命中正文。

健壮性原则：任何一个扫描服务失败或缺失 Key，都不能当作“已查无泄漏”。
本脚本会把两个服务的结果分开收集；失败的一方记为未知，成功的一方照常记录。
无论扫描是否完整，都会写出脱敏的 JSON 报告并给出终端摘要。
"""
import json
import sys

from sanjing import datapro, model
from sanjing.config import PROJECT_DIR

# 固定安全错误码：只允许这些枚举值出现在报告/终端里，避免泄露 traceback 或原始异常。
DEEPSEEK_SCAN_FAILED = "DEEPSEEK_SCAN_FAILED"
DATAPRO_KEY_UNAVAILABLE = "DATAPRO_KEY_UNAVAILABLE"
DATAPRO_SCAN_FAILED = "DATAPRO_SCAN_FAILED"
SECRET_LEAK_FOUND = "SECRET_LEAK_FOUND"
VERIFICATION_INCOMPLETE = "VERIFICATION_INCOMPLETE"


def _datapro_error_code(exc):
    """把 datapro 异常映射到固定白名单错误码，绝不回显异常原文或 Key。"""
    try:
        code = getattr(exc, "code", None)
        if type(code) is str and code == "KEY_UNAVAILABLE":
            return DATAPRO_KEY_UNAVAILABLE
    except Exception:  # noqa: BLE001 - 异常对象本身也可能提供不安全或出错的属性。
        pass
    return DATAPRO_SCAN_FAILED


def _is_non_negative_int(value):
    """只接受普通的非负整数；bool 虽是 int 子类，但不是有效扫描计数。"""
    return type(value) is int and value >= 0


def _safe_deepseek_result(result, path_count):
    """校验并仅保留 DeepSeek 扫描器的白名单字段。"""
    if type(result) is not dict:
        return None, False

    safe_result = {}
    verified = result.get("verified")
    scanned_files = result.get("scanned_files")
    matching_files = result.get("matching_files")

    # 失败结果也只保留类型安全的白名单值；reason、Key 或其他额外字段不进入报告。
    if type(verified) is bool:
        safe_result["verified"] = verified
    if _is_non_negative_int(scanned_files):
        safe_result["scanned_files"] = scanned_files
    if _is_non_negative_int(matching_files):
        safe_result["matching_files"] = matching_files

    valid = (
        verified is True
        and "scanned_files" in safe_result
        and "matching_files" in safe_result
        and scanned_files <= path_count
        and matching_files <= scanned_files
    )
    return safe_result or None, valid


def run_secret_scan(paths):
    """分别运行两个扫描服务并收集结果；任何一方失败都不会阻止另一方。"""
    paths = list(paths)
    path_count = len(paths)
    deepseek_result = None
    deepseek_status = None
    deepseek_error_code = None

    datapro_result = None
    datapro_status = None
    datapro_error_code = None

    try:
        raw_deepseek_result = model.check_secret_leaks(paths)
        deepseek_result, deepseek_valid = _safe_deepseek_result(raw_deepseek_result, path_count)
        if deepseek_valid:
            deepseek_status = "verified"
        else:
            deepseek_status = "error"
            deepseek_error_code = DEEPSEEK_SCAN_FAILED
    except Exception:  # noqa: BLE001 - 这里必须兜住所有异常，避免单个服务失败中断整体扫描。
        deepseek_result = None
        deepseek_status = "error"
        deepseek_error_code = DEEPSEEK_SCAN_FAILED

    try:
        raw_datapro_result = datapro.scan_key_leaks(paths)
        if _is_non_negative_int(raw_datapro_result):
            datapro_result = raw_datapro_result
            datapro_status = "verified"
        else:
            datapro_status = "error"
            datapro_error_code = DATAPRO_SCAN_FAILED
    except Exception as exc:  # noqa: BLE001
        datapro_status = "error"
        datapro_error_code = _datapro_error_code(exc)

    report = {
        "files_scanned": path_count,
        "deepseek": deepseek_result,
        "deepseek_status": deepseek_status,
        "deepseek_error_code": deepseek_error_code,
        "datapro_exact_key_occurrences": datapro_result,
        "datapro_status": datapro_status,
        "datapro_error_code": datapro_error_code,
        "scan_completed": deepseek_status == "verified" and datapro_status == "verified",
        "summary_codes": [],
        "notice": "扫描包含本地数据库、响应快照、HTML 与工程文件；不读取 Git 对象。",
    }

    # 汇总退出码：命中泄漏、任一服务失败或未完成，都不能安全通过。
    if deepseek_result and deepseek_result.get("matching_files", 0) > 0:
        report["summary_codes"].append(SECRET_LEAK_FOUND)
    if datapro_result:
        report["summary_codes"].append(SECRET_LEAK_FOUND)
    if deepseek_error_code:
        report["summary_codes"].append(deepseek_error_code)
    if datapro_error_code:
        report["summary_codes"].append(datapro_error_code)
    if not report["scan_completed"]:
        report["summary_codes"].append(VERIFICATION_INCOMPLETE)

    # 去重但保持顺序，让输出稳定。
    seen = set()
    report["summary_codes"] = [c for c in report["summary_codes"] if not (c in seen or seen.add(c))]
    return report


def write_report(report):
    """写出脱敏 JSON 报告并返回输出路径。"""
    output = PROJECT_DIR / "runtime" / "professional_verification" / "secret_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def print_terminal_summary(report, output_path):
    """给终端看的脱敏摘要：只含状态、计数和错误码，不含异常原文或命中内容。"""
    terminal = {
        "files_scanned": report["files_scanned"],
        "deepseek_status": report["deepseek_status"],
        "datapro_status": report["datapro_status"],
        "scan_completed": report["scan_completed"],
        "summary_codes": report["summary_codes"],
        "report_path": str(output_path),
    }
    print(json.dumps(terminal, ensure_ascii=False))


def main(argv=None):
    paths = [path for path in PROJECT_DIR.rglob("*")
             if path.is_file() and ".git" not in path.parts and ".venv" not in path.parts]
    report = run_secret_scan(paths)
    output_path = write_report(report)
    print_terminal_summary(report, output_path)

    # 退出策略：全部扫描成功且无命中才返回 0；否则一律非零，避免把未知当通过。
    if report["summary_codes"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
