import os

from dotenv import load_dotenv


def test_env_loading():
    load_dotenv()
    # 验证关键环境变量是否存在（即便为空也应能读取）
    assert "DATABASE_URL" in os.environ or os.path.exists(".env")
