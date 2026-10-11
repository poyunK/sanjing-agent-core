"""本次 Ark 接线验收：全脚本最多八次模型、四次专业查询，无自动重试。"""
import json
from datetime import datetime, timezone
from unittest.mock import patch

from sanjing import datapro, model
from sanjing.agent import run_turn
from sanjing.config import PROJECT_DIR


def main():
    directory = PROJECT_DIR / 'runtime' / 'ark_verification'
    directory.mkdir(parents=True, exist_ok=True)
    counts = {'model_requests': 0, 'data_queries': 0}
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'provider': 'ark',
              'model': 'ark-code-latest', 'endpoint': model.ARK_API_URL,
              'limits': {'model_requests': 8, 'data_queries': 4, 'max_output_tokens': 1500},
              'counts': counts, 'model_events': [], 'data_events': [], 'cases': [],
              'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
              'billing': '不是实际AFP扣费统计；失败或超时请求的用量可能未知，不自动重试。'}

    def save():
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def llm(messages, definitions):
        if counts['model_requests'] >= 8:
            raise model.ModelError('LIVE_MODEL_BUDGET')
        counts['model_requests'] += 1
        row = {'attempt': counts['model_requests']}
        try:
            result = model.ask_llm(messages, definitions, provider='ark', timeout_seconds=60)
            row.update(status='returned', tool_names=[c['function']['name'] for c in result['message'].get('tool_calls', [])],
                       usage=result.get('usage', {}))
            for key in report['usage']:
                value = result.get('usage', {}).get(key)
                if type(value) is int:
                    report['usage'][key] += value
            return result
        except model.ModelError as error:
            row.update(status='failed_or_unknown', error_code=error.code, usage=None)
            raise
        finally:
            report['model_events'].append(row)
            save()

    def data(query):
        if counts['data_queries'] >= 4:
            raise datapro.DataproError('LIVE_DATA_BUDGET')
        counts['data_queries'] += 1
        row = {'attempt': counts['data_queries']}
        try:
            result = datapro.query_data(query, timeout_seconds=60)
            row.update(status='returned', dataset_type=result.get('dataset_type'),
                       item_count=len(result.get('items', [])))
            return result
        except datapro.DataproError as error:
            row.update(status='failed_or_unknown', error_code=error.code)
            raise
        finally:
            report['data_events'].append(row)
            save()

    cases = [
        ('demo', '这是一条合成验收问题。请根据本地演示测评，只探索用户研究这个岗位。依次读测评、检索岗位、计算、提交建议。保持说明简短，不要在生成文字里编写分数，不调用专业数据。'),
        ('research', '这是合成验收，没有真实个人资料。仅查询一次学术资料：Big Five personality job performance meta-analysis。只采用一篇论文的标题或摘要字段。读取原文后，提交阅读原文和核对适用范围的职业探索建议。自由文字不要写数字，不断言人格决定职业，不读演示测评，不要查询其他类别。'),
    ]
    # 验收上限独立于用户以后配置的输出上限，不扩大本次预算。
    with patch.object(model, 'MAX_OUTPUT_TOKENS', 1500):
        for name, question in cases:
            if counts['model_requests'] >= 8:
                break
            result = run_turn(None, question, db_path=directory/'live.sqlite3', client=llm,
                              data_client=data, on_progress=lambda e: print(e['stage'], flush=True),
                              request_id='ark-acceptance-'+name)
            report['cases'].append({k: result[k] for k in ('session_id', 'turn_id', 'status', 'error_code', 'ledger_path', 'usage')})
            report['cases'][-1]['case'] = name
            save()
            print(json.dumps({'case': name, 'status': result['status'], 'error_code': result['error_code'], 'counts': counts}, ensure_ascii=False), flush=True)
            if result['error_code'] in ('HTTP_401', 'HTTP_403', 'KEY_INVALID', 'KEY_UNAVAILABLE'):
                break
    save()
    print(json.dumps({'counts': counts, 'usage': report['usage'], 'report': str(directory/'report.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
