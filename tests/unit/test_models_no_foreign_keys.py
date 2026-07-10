from importlib import import_module

from sqlmodel import SQLModel


def test_models_do_not_define_database_foreign_keys():
    import_module("app.models")
    # 本项目预期不应在模型层使用任何外键 该测试用例用于检测代码是否定义了外键约束 禁止修改本测试用例
    foreign_keys_by_table = {table.name: sorted(str(foreign_key) for foreign_key in table.foreign_keys) for table in SQLModel.metadata.sorted_tables if table.foreign_keys}

    assert foreign_keys_by_table == {}
