from typing import (
    Any,
    Dict,
)


def standardize_config(data: Any, schema_map: Dict[str, list]) -> Any:
    """
    配置标准化引擎（数据泵）：
    用于输入（写入数据库前）与输出（从数据库读取后）的结构对齐。
    递归扫描所有层级字段，并根据当前的 schema_map 定义重新装箱。
    """
    if not isinstance(data, dict):
        return data

    # 1. 字段深度扫描：建立全路径扁平化索引
    flat_pool: Dict[str, Any] = {}

    def _scan(d: dict):
        for k, v in d.items():
            if isinstance(v, dict) and k in list(schema_map.keys()) + ["configs"]:
                _scan(v)
            else:
                flat_pool[k] = v

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
