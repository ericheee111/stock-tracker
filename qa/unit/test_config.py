"""后端配置单测：_read_toml 行为（N3 修复验证）。

断言：
① 缺失文件 -> 返回空 dict {}（保留缺省语义，不抛异常）。
② 坏 TOML   -> 抛 ConfigError（不再静默降级回默认值）。

运行：
    cd /d/Projects/stock-tracker
    python -m unittest qa.unit.test_config -v
"""

import os
import tempfile
import unittest

from stock_tracker.core.config import ConfigError, _read_toml


class TestReadToml(unittest.TestCase):
    """_read_toml 契约：缺失兜底、解析失败显式报错。"""

    def test_missing_file_returns_empty_dict(self):
        """文件不存在时返回空 dict；保留缺省语义，不应抛异常。"""
        d = tempfile.mkdtemp()
        miss = os.path.join(d, "nope.toml")
        self.assertEqual(_read_toml(miss), {})

    def test_bad_toml_raises_config_error(self):
        """解析失败必须抛 ConfigError，而非静默回退默认值。"""
        d = tempfile.mkdtemp()
        bad = os.path.join(d, "bad.toml")
        with open(bad, "w", encoding="utf-8") as fh:
            # 重复 [a] 节 + 未结束字符串，必然触发 tomllib.TOMLDecodeError
            fh.write('[a]\nkey = "unterminated\n[duplicate]\n[a]\n')
        with self.assertRaises(ConfigError):
            _read_toml(bad)

    def test_valid_toml_parsed(self):
        """健全性回归：合法 TOML 正常返回 dict。"""
        d = tempfile.mkdtemp()
        good = os.path.join(d, "ok.toml")
        with open(good, "w", encoding="utf-8") as fh:
            fh.write('[server]\nhost = "127.0.0.1"\nport = 9090\n')
        data = _read_toml(good)
        self.assertEqual(data["server"]["host"], "127.0.0.1")
        self.assertEqual(data["server"]["port"], 9090)


if __name__ == "__main__":
    unittest.main()
