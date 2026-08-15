import copy

import ai_stock.default_config as default_config
from typing import Dict, Optional

# Use default config but allow it to be overridden
_config: Optional[Dict] = None


def initialize_config():
    """Initialize the configuration with default values."""
    global _config
    if _config is None:
        # deepcopy 而非 .copy()：浅拷贝会让嵌套 dict（data_vendors /
        # tool_vendors / role_llms）与 DEFAULT_CONFIG 共享引用，一次
        # set_config 定制就永久污染全局默认值（同进程后续所有 run 都受影响）。
        _config = copy.deepcopy(default_config.DEFAULT_CONFIG)


def set_config(config: Dict):
    """Update the configuration with custom values."""
    global _config
    initialize_config()
    # 同理 deepcopy 传入值：调用方事后修改自己那份 config 不应再回写到这里。
    _config.update(copy.deepcopy(config))


def get_config() -> Dict:
    """Get the current configuration."""
    if _config is None:
        initialize_config()
    return copy.deepcopy(_config)


# Initialize with default config
initialize_config()
