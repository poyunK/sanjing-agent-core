"""受限 Key 文件解析的异常形状；全部使用合成字符串。"""
import tempfile
import unittest
from pathlib import Path
from sanjing.key_loader import KeyFileError, load_ark_key


class KeyLoaderBoundsTests(unittest.TestCase):
    def test_reject_yaml_tags_arrays_duplicate_keys_and_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'synthetic.yaml'
            for content in ('api_key: [one]', 'api_key: !!str', 'api_key: a\napi_key: b',
                            'api_key:\napi_key: test', 'x' * 8193):
                with self.subTest(shape=content[:20]):
                    filename.write_text(content, encoding='utf-8')
                    with self.assertRaises(KeyFileError) as caught:
                        load_ark_key(filename)
                    self.assertEqual(caught.exception.code, 'KEY_INVALID')

    def test_valid_bom_and_quotes_are_not_in_value(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'synthetic.yaml'
            filename.write_text('\ufeffapi_key: "test-value_01"\n', encoding='utf-8')
            self.assertEqual(load_ark_key(filename), 'test-value_01')
