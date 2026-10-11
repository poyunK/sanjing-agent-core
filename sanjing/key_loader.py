"""从工程外部读取火山 Ark Key，不把文件内容写入日志或异常。"""

import json
import re
from pathlib import Path


class KeyFileError(Exception):
    """只携带安全错误码，不包含文件内容。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


_KEY_FIELDS = {
    "api_key", "ark_api_key", "ark_key", "volcengine_api_key", "key", "token",
}
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _scalar(value):
    """读取本任务需要的简单 YAML 字符串标量，不执行 YAML 标签或对象构造。"""
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            raise KeyFileError("KEY_INVALID") from None
        if not isinstance(parsed, str):
            raise KeyFileError("KEY_INVALID")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise KeyFileError("KEY_INVALID")
        return value[1:-1].replace("''", "'")
    # YAML 的行内注释必须由空白引出；Key 本身不允许空白。
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return value


def _parse_key_text(text):
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise KeyFileError("KEY_INVALID")

    # 兼容旧的纯文本 Key 文件；扩展名不决定内容格式。
    if "\n" not in text and "\r" not in text and not re.match(r"^[A-Za-z][A-Za-z0-9_-]*\s*:", text):
        candidate = text
    else:
        fields, seen_fields = {}, set()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition(":")
            if not separator or not _FIELD_NAME.fullmatch(name.strip()):
                raise KeyFileError("KEY_INVALID")
            normalized = name.strip().lower().replace("-", "_")
            if normalized in _KEY_FIELDS:
                if normalized in seen_fields:
                    raise KeyFileError('KEY_INVALID')
                seen_fields.add(normalized)
                parsed = _scalar(value)
                if parsed:
                    fields[normalized] = parsed
        values = set(fields.values())
        if len(values) != 1:
            raise KeyFileError("KEY_INVALID")
        candidate = values.pop()

    if not candidate or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', candidate):
        raise KeyFileError("KEY_INVALID")
    return candidate


def load_ark_key(key_file):
    """读取纯文本或受限 mapping 格式的火山 Key。"""
    try:
        with Path(key_file).open('rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise KeyFileError('KEY_INVALID')
    except (OSError, TypeError, ValueError):
        raise KeyFileError("KEY_UNAVAILABLE") from None
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise KeyFileError('KEY_INVALID') from None
    return _parse_key_text(text)
