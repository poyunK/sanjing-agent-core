"""本机交付前扫描：密钥仍只由各请求模块读取，报告不包含密钥和命中正文。"""
import json

from sanjing import datapro, model
from sanjing.config import PROJECT_DIR


def main():
    paths = [path for path in PROJECT_DIR.rglob("*")
             if path.is_file() and ".git" not in path.parts and ".venv" not in path.parts]
    deepseek = model.check_secret_leaks(paths)
    data_hits = datapro.scan_key_leaks(paths)
    report = {"files_scanned": len(paths), "deepseek": deepseek,
              "datapro_exact_key_occurrences": data_hits,
              "notice": "扫描包含本地数据库、响应快照、HTML 与工程文件；不读取 Git 对象。"}
    output = PROJECT_DIR / "runtime" / "professional_verification" / "secret_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if data_hits or deepseek.get("matching_files", 0) or not deepseek.get("verified", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
