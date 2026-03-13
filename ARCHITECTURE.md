# MonoLight 架构说明

## 1. 核心设计理念
MonoLight 采用“后端管控、前端透明”的设计哲学。系统的核心是 **Profile 驱动架构**：通过在数据库中定义并激活具体的 Profile，系统会自动补全所有推理参数，将前端调用从繁琐的 API 细节中解放出来。

## 2. 核心模块职责

### 📂 `app/core/dispatcher.py`
调度大脑。包含 `ChatDispatcher`，负责根据当前激活的 Profile 自动填充模型 ID、温度、最大 Token 等参数，实现业务逻辑的分发。

### 📂 `app/models/profile.py` & `app/schemas/profile.py`
配置管理中心。定义了模型配置的存储结构，支持多提供商、多模型配置的动态切换。

### 📂 `app/api/v1/chat.py`
精简对话接口。执行“文本入、标准响应出”的逻辑，完全屏蔽底层推理细节。

### 📂 `app/transformers/openai.py`
标准化响应转换器。将后端的推理结果统一转化为 OpenAI 兼容格式，确保护接前端的通用性。

## 3. 数据处理流
1. 前端发送极简 Body (`message` + `stream`)。
2. `ChatDispatcher` 从数据库读取激活的 `Profile`。
3. 结合用户消息与 Profile 参数构造完整的请求负载。
4. 调用对应适配器获取响应。
5. `Transformer` 统一输出格式。