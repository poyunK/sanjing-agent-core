"""搜索的离线教学模型：合成资料，仍走真实的工具校验与记账入口。"""
import json
from .offline import _reply


def demo_search_client(query, **options):
    """不读 Key、不联网；返回和豆包客户端相同形状的字典。"""
    return {'code': 0, 'dataset_type': 'web_search', 'trace_id': 'offline-web-demo',
            'items': [{'title': '合成招聘页面（不是实际职位）',
                       'summary': '演示岗位需要访谈记录与资料核对；这段文字为合成摘要。',
                       'url': 'https://example.org/sanjing-demo',
                       'provider': '合成网页搜索演示', 'published_at': '2026-01-01',
                       'text_scope': '搜索摘要，未阅读全文'}]}


def ask_web_demo(messages, definitions):
    """模拟查询 → 读摘要字段 → 提交建议，让学习者观察三个请求之间的字典变化。"""
    user_index = max(i for i, row in enumerate(messages) if row['role'] == 'user')
    names, results = {}, []
    for row in messages[user_index + 1:]:
        if row['role'] == 'assistant':
            for call in row.get('tool_calls', []):
                names[call['id']] = call['function']['name']
        elif row['role'] == 'tool':
            results.append((names.get(row['tool_call_id']), json.loads(row['content'])))
    if not results:
        return _reply([('search_web', {'query': '合成演示岗位要求', 'count': 1})])
    query = next((result for name, result in results if name == 'search_web' and result.get('ok')), None)
    if not query or not query['data'].get('items'):
        return _reply([('ask_user', {'fields': ['goal']})])
    evidence_id = query['data']['items'][0]['evidence_id']
    if not any(name == 'get_record' and result.get('ok') for name, result in results):
        return _reply([('get_record', {'record_type': 'evidence', 'record_id': evidence_id,
                                     'field_path': '/record/summary'})])
    citations = [{'evidence_id': evidence_id, 'field_path': '/record/summary'}]
    return _reply([('submit_decision', {
        'title': '网页搜索账本的离线练习', 'options': ['核对官方岗位要求', '补充经历证据'],
        'claims': [{'kind': 'fact', 'text': '合成页面的摘要字段', 'citations': citations},
                   {'kind': 'inference', 'text': '先核对岗位全文，再用自身经历判断适合程度。',
                    'citations': citations}],
        'unknowns': ['这只是合成摘要，不能作为实际招聘依据', '没有阅读全文或核实招聘是否仍有效'],
        'next_actions': ['打开账本，核对摘要、链接、读取事件与采用字段']})])
