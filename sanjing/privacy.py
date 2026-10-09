"""记录之前遮住常见密钥形式；不会为了脱敏额外读取 Key 文件。"""

import re


def redact(value):
    """返回脱敏副本；字符串、嵌套列表与字典都可传入。"""
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[密钥已隐藏]", value)
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "Bearer [已隐藏]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[已隐藏]" if str(key).lower() in {"api_key", "authorization", "secret"}
            else redact(item) for key, item in value.items()
        }
    return value

