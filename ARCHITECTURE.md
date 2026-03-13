# MonoLight Architecture

## 1. Core Philosophy
MonoLight is an asynchronous-driven, modular, and hierarchical AI interaction framework. It aims for high-performance message dispatching and scalable Agent capabilities.

## 2. Technology Stack
* **Web Framework:** [FastAPI](https://fastapi.tiangolo.com/)
* **Runtime:** Python 3.10+
* **Database:** SQLAlchemy with SQLite
* **Asynchronous I/O:** Fully async processing pipeline

## 3. Module Responsibilities

### 📂 `main.py`
Entry point. Initializes the FastAPI app, mounts routers, and manages the database engine lifecycle.

### 📂 `app/core/`
The brain of the framework. Contains the **Dispatcher**, responsible for routing incoming messages to specific handlers.

### 📂 `app/adapters/`
Unified interfaces for different platforms. Ensures the core logic remains decoupled from platform-specific APIs.

### 📂 `app/transformers/`
Handles data conversion between platform-specific payloads and internal standardized message formats.

### 📂 `app/models/` & `app/providers/`
* **Models:** SQLAlchemy ORM definitions.
* **Providers:** Infrastructure logic like database connections and external API clients.

### 📂 `app/api/` & `app/schemas/`
* **API:** RESTful endpoints for external interaction.
* **Schemas:** Pydantic models for request/response validation.

## 4. Data Flow
`External Message` -> `Adapters` -> `Transformers` -> `Dispatcher` -> `Business Logic / Agents` -> `Response Output`
