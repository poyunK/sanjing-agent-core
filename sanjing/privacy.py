"""记录之前遮住常见密钥形式；不会为了脱敏额外读取 Key 文件。"""

import re


def redact(value, *, secrets=()):
    """返回脱敏副本；字符串、嵌套列表与字典都可传入。"""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[密钥已隐藏]")
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[密钥已隐藏]", value)
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "Bearer [已隐藏]", value)
    if isinstance(value, list):
        return [redact(item, secrets=secrets) for item in value]
    if isinstance(value, dict):
        return {
            redact(str(key), secrets=secrets): "[已隐藏]"
            if re.sub(r"[-_]", "", str(key).lower()) in {
                "apikey", "authorization", "secret", "xagentplankey", "token", "password",
                "anthropicauthtoken", "cookie"}
            else redact(item, secrets=secrets) for key, item in value.items()
        }
    return value

