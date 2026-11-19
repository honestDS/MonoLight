# Monolight 跨系统路径策略与安全校验规范

## 1. 通用路径表达方式（无硬编码绝对路径）

为保证本技能在 Windows、macOS 与 Linux 的任意 Monolight 部署目录下都能即插即用，技能文档与脚本采用相对结构定位：

1. **包内位置关系**：本技能位于 Monolight 目录树的 `data/skills/skillhub-manual-installer/` 下，因此 Monolight 技能根目录即本技能包的上一级目录（`..`），在脚本内通过 `Path(__file__).resolve().parents[2]` 动态求得。
2. **工作区回退查找**：若脚本被放到独立目录测试，也会从 `Path.cwd()` 向上逐级查找 `data/skills/` 子目录（例如从 `temp/<session_id>/` 向上匹配 `../../data/skills`）。
3. **显式覆盖通道**：支持通过 `--skills-dir` 参数或 `MONOLIGHT_SKILLS_DIR`、`AGENT_SKILLS_DIR`、`SKILLS_DIR` 环境变量覆盖目标目录。
4. **多平台探测兜底**：保留 `detect-dirs` 命令，可列出 Monolight 技能根目录及当前用户主目录下的常见 Agent 技能目录，便于跨工具迁移。

## 2. 不依赖 web_tool 的官方接口清单

脚本仅使用 Python 3 标准库（`urllib.request`、`urllib.parse`、`json`、`zipfile`、`tempfile`、`pathlib`、`shutil`）直接访问 SkillHub 公开接口：

- 技能搜索：`GET https://api.skillhub.cn/api/skills?page=1&pageSize=10&keyword=...`
- 分类列表：`GET https://api.skillhub.cn/api/v1/categories`
- 技能详情与安全报告：`GET https://api.skillhub.cn/api/v1/skills/<slug>?namespace=<namespace>`
- 压缩包直链下载：`GET https://api.skillhub.cn/api/v1/download?slug=<slug>&namespace=<namespace>`

## 3. 安全校验规则

1. **Zip-Slip 路径穿越拦截**：拒绝所有以 `/` 或 `\` 开头、含驱动器盘符（如 `C:`）、含 `..` 父级目录或空字节的压缩包条目，并逐条核验解压目标路径严格位于暂存根目录之内。
2. **体量防爆**：限制最大下载体积（50 MB）、最大解压总体积（150 MB）与最大条目数（2000）。
3. **元数据核验**：压缩包根层（或唯一顶层包装目录内）必须包含 `SKILL.md` / `skill.md`，且 frontmatter 中包含非空的 `name` 与 `description`。
4. **安全报告直读**：通过技能详情接口提取 `securityReports` 字段，明确输出各安全引擎（如 `keen`、`sanbu`）的状态、中文说明与报告链接。
5. **暂存原子替换**：覆盖已有技能目录（`--force`）时先在技能根目录内的隐藏临时目录完成全量解压与校验，再备份旧目录并移入新目录，出错自动回滚。
