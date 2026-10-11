"""有上限的真实搜索验收：最多两次豆包搜索、四次 Ark 请求，不调用 DataPro。

仅使用公开页面查询，不输出响应原文或凭证。报告保存在被 Git 忽略的 runtime。
"""
import json
import time
from pathlib import Path

from sanjing import model, store
from sanjing.agent import run_turn
from sanjing.config import PROJECT_DIR
from sanjing.doubao_search import search_web, SearchError


def main():
    output = PROJECT_DIR / 'runtime' / 'search_verification'
    output.mkdir(parents=True, exist_ok=True)
    db = output / 'agent.sqlite3'
    actual = {'model_requests': 0, 'search_requests': 0, 'search_statuses': [], 'total_tokens': 0}
    selected = {'search_web', 'get_record', 'submit_decision', 'ask_user'}

    def ask(messages, definitions):
        if actual['model_requests'] >= 4:
            raise model.ModelError('LIVE_BUDGET_EXCEEDED')
        actual['model_requests'] += 1
        reply = model.ask_llm(messages, [tool for tool in definitions if tool['function']['name'] in selected], provider='ark')
        actual['total_tokens'] += reply.get('usage', {}).get('total_tokens', 0)
        return reply

    def query(text, **options):
        if actual['search_requests'] >= 2:
            raise SearchError('LIVE_BUDGET_EXCEEDED')
        actual['search_requests'] += 1
        try:
            payload = search_web(text, **options)
            actual['search_statuses'].append({'status': 'returned', 'records': len(payload['items'])})
            return payload
        except SearchError as error:
            actual['search_statuses'].append({'status': 'error', 'code': error.code})
            raise

    def no_data(_query, **_options):
        raise SearchError('DATAPRO_DISABLED_FOR_THIS_CHECK')

    session = store.ensure_session(db)
    start = time.monotonic()
    result = run_turn(session,
        '使用 search_web 搜索字节跳动官方校园招聘入口，只搜索一次，count 为一，sites 为 jobs.bytedance.com。'
        '然后用 get_record 读取第一条候选的 /record/url 和 /record/title。'
        '最后用 submit_decision 提交“核对官方招聘入口”的核查建议，事实只引用读取的字段；'
        '未知项明确说明没有阅读全文、没有核实具体职位或当前招录状态。不要查询专业数据集。',
        db_path=db, client=ask, search_client=query, data_client=no_data,
        on_progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    report = {**actual, 'status': result['status'], 'error_code': result.get('error_code'),
              'elapsed_seconds': round(time.monotonic() - start, 2),
              'usage': result['usage'], 'ledger_path': result['ledger_path'],
              'notice': 'token 为接口报告值；未核验 AFP 扣费；仅核对公开入口，不证明职位有效。'}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
