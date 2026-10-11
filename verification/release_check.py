"""发布前检查工作文件、Git 暂存对象和全部可达历史对象中的实际凭证。

请求模块负责读取工程外 Key，本脚本只收集待扫描字节并输出计数。
Git、扫描或缺失凭证的错误均阻止发布；报告不包含命中原文。
"""
import json
import subprocess
import tempfile
from pathlib import Path

from sanjing import model, datapro
from sanjing.config import PROJECT_DIR


def git(*arguments):
    return subprocess.check_output(['git', *arguments], cwd=PROJECT_DIR, stderr=subprocess.DEVNULL)


def scan(paths):
    paths = list(paths)
    result = {'files': len(paths)}
    for provider in ('deepseek', 'ark'):
        report = model.check_secret_leaks(paths, provider=provider)
        if report.get('verified') is not True:
            raise RuntimeError('SCAN_INCOMPLETE')
        result[provider + '_matching_files'] = report['matching_files']
    result['datapro_occurrences'] = datapro.scan_key_leaks(paths)
    # 旧火山纯文本凭证仍在工程外时也核对，避免新版 Key 替换后遗漏历史值。
    old_key = Path.home() / 'Documents' / '火山keyUTF8.txt'
    if old_key.is_file():
        result['previous_ark_occurrences'] = datapro.scan_key_leaks(paths, key_file=old_key)
    return result


def main():
    output = PROJECT_DIR / 'runtime' / 'release_audit'
    output.mkdir(parents=True, exist_ok=True)
    work_files = [path for path in PROJECT_DIR.rglob('*') if path.is_file()
                  and not any(part in ('.git', '.venv', '__pycache__', 'release_audit') for part in path.parts)]
    index = []
    forbidden = []
    for entry in git('ls-files', '-s', '-z').split(b'\0'):
        if not entry:
            continue
        header, name = entry.split(b'\t', 1)
        _mode, oid, stage = header.split()
        path = name.decode('utf-8')
        if stage != b'0':
            raise RuntimeError('INDEX_CONFLICT')
        index.append(oid.decode('ascii'))
        parts = Path(path).parts
        lower = path.lower()
        if (any(part in ('runtime', '.handoff-local', '.venv', '__pycache__') for part in parts)
                or lower.endswith(('.sqlite3', '.log')) or Path(path).name.startswith('.env')
                or ('key' in Path(path).stem.lower() and lower.endswith(('.txt', '.yaml', '.yml')))):
            forbidden.append(path)
    history = set()
    for line in git('rev-list', '--objects', '--all').splitlines():
        oid = line.split(b' ', 1)[0].decode('ascii')
        if git('cat-file', '-t', oid).strip() == b'blob':
            history.add(oid)
    report = {'forbidden_index_paths': forbidden, 'working': scan(work_files)}
    with tempfile.TemporaryDirectory(dir=output) as temp:
        files = {}
        for oid in set(index) | history:
            path = Path(temp) / oid
            path.write_bytes(git('cat-file', 'blob', oid))
            files[oid] = path
        report['index'] = scan(files[oid] for oid in set(index))
        report['history'] = scan(files[oid] for oid in history)
    report['passed'] = not forbidden and all(
        all(value == 0 for key, value in row.items() if key != 'files')
        for row in (report['working'], report['index'], report['history']))
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('RELEASE_SCAN_FAILED：未确认无泄漏，停止发布；没有输出凭证或异常正文。')
        raise SystemExit(1) from None
