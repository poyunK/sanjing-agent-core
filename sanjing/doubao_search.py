"""豆包网页搜索客户端；参考现成 doubao-search 技能，运行仅需 Python 标准库。

Key 共用 DataPro 的工程外文件；固定服务地址，不抓取结果中的任意 URL。
"""
import json
import math
import queue
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

from .datapro import _load_key
from .privacy import redact

SEARCH_URL = 'https://open.feedcoopapi.com/search_api/web_search'
MAX_RESPONSE_BYTES = 1_048_576
TIME_RANGES = ('', 'OneDay', 'OneWeek', 'OneMonth', 'OneYear')
DOMAIN_PATTERN = r'(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}'


class SearchError(Exception):
    """只返回固定错误码，不回显供应商正文、密钥或异常原文。"""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise SearchError('REDIRECT_BLOCKED')


def build_request_body(query, count=5, sites=None, time_range='', authority=0):
    """模型只给检索条件；地址、鉴权、搜索类型和是否返回原文链接由程序固定。"""
    if not isinstance(query, str) or not query.strip() or len(query) > 100:
        raise SearchError('INVALID_QUERY')
    if type(count) is not int or not 1 <= count <= 10:
        raise SearchError('INVALID_COUNT')
    if time_range not in TIME_RANGES or type(authority) is not int or authority not in (0, 1):
        raise SearchError('INVALID_FILTER')
    sites = [] if sites is None else sites
    if not isinstance(sites, list) or len(sites) > 5 or any(
            not isinstance(site, str) or not re.fullmatch(DOMAIN_PATTERN, site) for site in sites):
        raise SearchError('INVALID_SITES')
    filters = {'NeedUrl': True}
    if sites:
        filters['Sites'] = '|'.join(sorted(set(site.lower() for site in sites)))
    if authority:
        filters['AuthInfoLevel'] = authority
    body = {'Query': query.strip(), 'SearchType': 'web', 'Count': count, 'Filter': filters}
    if time_range:
        body['TimeRange'] = time_range
    return body


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(char) < 32 for char in value):
        return False
    try:
        url = urllib.parse.urlsplit(value)
        return url.scheme.lower() in ('https', 'http') and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        return False


def normalize_response(payload, count=5):
    """保留供应商标题、摘要、URL 和日期，映射到现有外部证据管道。

    原始字段名保存在 original_fields。摘要不会被当成网页全文。
    """
    if not isinstance(payload, dict):
        raise SearchError('BAD_RESPONSE')
    metadata = payload.get('ResponseMetadata', {})
    if not isinstance(metadata, dict):
        raise SearchError('BAD_RESPONSE')
    error = metadata.get('Error')
    if error:
        code = error.get('CodeN', error.get('Code')) if isinstance(error, dict) else None
        safe_code = str(code) if type(code) in (int, str) and re.fullmatch(r'[0-9]{3,6}', str(code)) else 'UNKNOWN'
        raise SearchError('SEARCH_BUSINESS_' + safe_code)
    result = payload.get('Result')
    if not isinstance(result, dict) or not isinstance(result.get('WebResults'), list):
        raise SearchError('BAD_RESPONSE')
    items, omitted = [], 0
    mapping = {'Title': 'title', 'Url': 'url', 'Summary': 'summary', 'Snippet': 'snippet',
               'PublishTime': 'published_at', 'SiteName': 'site_name', 'Id': 'record_id',
               'AuthInfoLevel': 'authority_level', 'AuthInfoDes': 'authority_description'}
    for row in result['WebResults']:
        if not isinstance(row, dict):
            raise SearchError('BAD_RESPONSE')
        if not isinstance(row.get('Title'), str) or not row['Title'].strip() or not _safe_url(row.get('Url')):
            omitted += 1
            continue
        item = {'provider': '豆包搜索（网页检索）', 'source_type': 'web_search', 'original_fields': {}}
        for original, target in mapping.items():
            value = row.get(original)
            if isinstance(value, str) or type(value) in (int, float):
                item[target] = value
                item['original_fields'][original] = value
        item['text_scope'] = '搜索摘要或片段，未阅读全文'
        items.append(item)
        if len(items) >= count:
            break
    trace = result.get('LogId') or metadata.get('RequestId')
    return {'code': 0, 'dataset_type': 'web_search', 'items': items,
            'trace_id': trace if isinstance(trace, str) else None,
            'omitted_invalid_results': omitted}


def search_web(query, *, count=5, sites=None, time_range='', authority=0, timeout_seconds=60, key_file=None):
    """一次只读搜索，无重试；返回值供工具记账，Key 不进入该返回值。"""
    body = build_request_body(query, count, sites, time_range, authority)
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60):
        raise SearchError('INVALID_TIMEOUT')
    try:
        key = _load_key(key_file)
    except Exception as error:
        code = getattr(error, 'code', None)
        raise SearchError(code if code in ('KEY_INVALID', 'KEY_UNAVAILABLE') else 'KEY_UNAVAILABLE') from None
    request = urllib.request.Request(SEARCH_URL, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, method='POST')
    results = queue.Queue(maxsize=1)
    def once():
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SearchError('RESPONSE_TOO_LARGE')
            payload = redact(json.loads(raw.decode('utf-8')), secrets=(key,))
            results.put((normalize_response(payload, count), None))
        except SearchError as error:
            results.put((None, error.code))
        except urllib.error.HTTPError as error:
            results.put((None, 'HTTP_' + str(error.code)))
        except (TimeoutError, socket.timeout):
            results.put((None, 'SEARCH_TIMEOUT'))
        except urllib.error.URLError:
            results.put((None, 'NETWORK_ERROR'))
        except Exception:
            results.put((None, 'BAD_RESPONSE'))
    threading.Thread(target=once, daemon=True).start()
    try:
        result, error = results.get(timeout=timeout_seconds)
    except queue.Empty:
        raise SearchError('SEARCH_TIMEOUT') from None
    if error:
        raise SearchError(error)
    return result
