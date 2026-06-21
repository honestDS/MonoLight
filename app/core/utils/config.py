from typing import (
    Any,
)


def standardize_config(data: Any, schema_map: dict[str, list]) -> Any:
    """
    配置标准化引擎（数据泵）：
    用于输入（写入数据库前）与输出（从数据库读取后）的结构对齐。
    递归扫描所有层级字段，并根据当前的 schema_map 定义重新装箱。
    """
    if not isinstance(data, dict):
        return data

    # 1. 字段深度扫描：建立全路径扁平化索引
    flat_pool: dict[str, Any] = {}

    nested_config_keys = set(schema_map.keys()) | {"configs"}

    def _scan(config_data: dict):
        for key, value in config_data.items():
            if isinstance(value, dict) and key in nested_config_keys:
                _scan(value)
            else:
                flat_pool[key] = value

    _scan(data)

    # 2. 结构强制重组：根据当前代码定义的 Schema 提取字段
    new_data = {}
    for group, fields in schema_map.items():
        group_data = data.get(group, {}) if isinstance(data.get(group), dict) else {}
        for field in fields:
            # 优先级：当前路径匹配 > 全局池同名匹配
            if field not in group_data and field in flat_pool:
                group_data[field] = flat_pool[field]
        new_data[group] = group_data

    return new_data
