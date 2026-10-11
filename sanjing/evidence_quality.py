"""只提示资料的适用限制，不给资料打一个冒充真实概率的可靠性分数。"""


def quality_flags(data):
    """输入为 professional_record.data；返回程序产生的代码和中文提示。

    获取时间只证明何时查询。日期、主体编号、摘要等仍须人核对含义。
    老账目也可调用此函数渲染提示，但不会改写保存时的原始依据。
    """
    record = data.get('record') if isinstance(data.get('record'), dict) else {}
    source = data.get('source') if isinstance(data.get('source'), dict) else {}
    category = data.get('category')
    flags = []

    def add(code, message):
        flags.append({'code': code, 'message': message})

    add('scope_not_proven', '字段引用检查只验证记录关联；资料是否支持分析仍需核对。')
    dates = source.get('time_fields') or source.get('published_at') or source.get('statistical_period') or source.get('year')
    if dates:
        add('check_time_scope', '原文日期、统计期或年份不代表当前状况；获取时间不等于发布时间。')
    else:
        add('time_unknown', '未提供可定位的资料日期，不能判断是否适合当前决策；获取时间不替代资料日期。')
    if not source.get('url'):
        add('source_link_missing', '缺少原始来源链接，需要通过记录编号或供应商资料另行核查。')
    if not data.get('route_verified'):
        add('data_type_unknown', '返回资料类型尚未确认，不能按请求的类别当成事实。')
    if category == 'web_search':
        add('search_summary_only', '豆包结果仅为搜索摘要或片段，未阅读全文；排序与权威标签不证明内容正确。')
    summaries = ('abstract', 'snippet', 'summary', '摘要', '简介')
    full_text = ('full_text', 'fulltext', '正文', '全文')
    if category == 'research' and any(record.get(k) for k in summaries) and not any(record.get(k) for k in full_text):
        add('summary_only', '目前只有摘要或检索片段；未阅读全文，不能推断方法质量、因果关系或个人适用性。')
    if category in ('company_profile', 'company_risk', 'public_financials'):
        entity_keys = ('unified_social_credit_code', 'social_credit_code', 'credit_code', 'uscc',
                       '统一社会信用代码', 'company_id', 'enterprise_id', '公司ID', '企业ID', 'stock_code', '证券代码')
        if not any(record.get(k) for k in entity_keys):
            add('entity_unresolved', '没有明确的企业主体标识；名称或结果行编号不足以排除同名、集团与子公司差异。')
        if category == 'company_risk':
            add('risk_coverage_unknown', '查询覆盖范围可能有限；未列出风险事件不等于企业无风险。')
    if category in ('industry', 'public_financials'):
        if not source.get('statistical_period'):
            add('period_missing', '缺少明确统计期或报告期，不能把不同时间的数据直接比较。')
        if not source.get('unit'):
            add('unit_missing', '缺少单位或币种，不能仅凭数值比较。')
        add('check_measurement_scope', '比较前须核对地区、指标定义、单位与统计口径是否一致。')
    return flags
