# Embedding 模块使用示例

## 快速开始

### 1. 配置环境变量

在 `.env` 文件中添加以下配置：

```env
# OpenAI Embedding 配置
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL_ID=text-embedding-3-small
EMBEDDING_API_KEY=sk-your-api-key-here
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_BATCH_SIZE=100
EMBEDDING_DIMENSIONS=1536
EMBEDDING_TIMEOUT=30

# 或使用本地模型
# EMBEDDING_PROVIDER=local
# EMBEDDING_MODEL_ID=sentence-transformers/all-MiniLM-L6-v2
# EMBEDDING_MODEL_CACHE_DIR=./models_cache
```

### 2. 使用 OpenAI API

```python
import asyncio
from app.embedding import EmbeddingClient, EmbeddingConfig

async def main():
    # 方式 1: 从环境变量加载配置
    from app.embedding.config import load_embedding_config
    config = load_embedding_config()
    
    # 方式 2: 手动创建配置
    config = EmbeddingConfig(
        provider_type="openai",
        model_id="text-embedding-3-small",
        api_key="sk-xxx",
        base_url="https://api.openai.com/v1"
    )
    
    # 创建客户端
    client = EmbeddingClient(config)
    
    # 单个文本向量化
    text = "MonoLight 是一个通用自主智能体基座"
    vector = await client.embed_single(text)
    print(f"向量维度: {len(vector)}")
    print(f"前5个值: {vector[:5]}")
    
    # 批量文本向量化
    texts = [
        "人工智能正在改变世界",
        "深度学习是AI的核心技术",
        "MonoLight支持多模态大模型"
    ]
    response = await client.embed(texts)
    print(f"\n批量处理结果:")
    print(f"模型: {response.model}")
    print(f"向量数量: {len(response.embeddings)}")
    print(f"向量维度: {response.dimensions}")
    print(f"Token使用: {response.usage}")

if __name__ == "__main__":
    asyncio.run(main())
```

### 3. 使用本地模型

```python
import asyncio
from app.embedding import EmbeddingClient, EmbeddingConfig

async def main():
    # 配置本地模型
    config = EmbeddingConfig(
        provider_type="local",
        model_id="sentence-transformers/all-MiniLM-L6-v2",
        batch_size=32,
        model_cache_dir="./models_cache"
    )
    
    client = EmbeddingClient(config)
    
    # 使用方式与 OpenAI 完全一致
    text = "这是一段测试文本"
    vector = await client.embed_single(text)
    print(f"本地模型向量维度: {len(vector)}")

if __name__ == "__main__":
    asyncio.run(main())
```

### 4. 相似度计算

```python
from app.embedding.utils import (
    cosine_similarity,
    batch_similarity,
    normalize_vector
)

# 计算两个向量的余弦相似度
vec1 = [1.0, 2.0, 3.0]
vec2 = [2.0, 3.0, 4.0]
similarity = cosine_similarity(vec1, vec2)
print(f"余弦相似度: {similarity}")

# 批量相似度计算
query = [1.0, 0.0, 0.0]
candidates = [
    [0.9, 0.1, 0.0],
    [0.0, 1.0, 0.0],
    [0.8, 0.2, 0.0],
]
results = batch_similarity(query, candidates)
print(f"最相似的前3个: {results[:3]}")

# 向量归一化
vec = [3.0, 4.0]
normalized = normalize_vector(vec)
print(f"归一化向量: {normalized}")
```

### 5. 集成到现有项目

```python
from fastapi import APIRouter, Depends
from app.embedding import EmbeddingClient
from app.embedding.config import load_embedding_config

router = APIRouter()

# 全局单例（推荐）
_embedding_client = None

def get_embedding_client() -> EmbeddingClient:
    global _embedding_client
    if _embedding_client is None:
        config = load_embedding_config()
        _embedding_client = EmbeddingClient(config)
    return _embedding_client

@router.post("/api/v1/embeddings")
async def create_embeddings(
    texts: list[str],
    client: EmbeddingClient = Depends(get_embedding_client)
):
    response = await client.embed(texts)
    return {
        "embeddings": response.embeddings,
        "model": response.model,
        "usage": response.usage
    }
```

## 支持的模型

### OpenAI
- `text-embedding-3-small` (推荐，性价比高)
- `text-embedding-3-large`
- `text-embedding-ada-002`

### 本地模型 (sentence-transformers)
- `sentence-transformers/all-MiniLM-L6-v2` (轻量级)
- `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (多语言)
- `BAAI/bge-small-zh-v1.5` (中文优化)
- 更多模型请访问: https://huggingface.co/models?library=sentence-transformers

## 性能建议

1. **批处理**: 尽量使用 `embed()` 批量处理而非多次调用 `embed_single()`
2. **缓存**: 对于频繁查询的文本，建议缓存其向量结果
3. **本地模型**: 首次使用会自动下载，建议配置 `model_cache_dir` 指定缓存目录
4. **超时设置**: 大批量处理时适当增加 `timeout` 值

## 错误处理

```python
from app.core.exceptions import LLMException, ServerException

try:
    vector = await client.embed_single("测试文本")
except LLMException as e:
    print(f"API 调用失败: {e.message}")
except ServerException as e:
    print(f"本地模型错误: {e.message}")
except ValueError as e:
    print(f"参数错误: {str(e)}")
```
