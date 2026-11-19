---
name: skillhub-manual-installer
description: "Monolight 内置技能管理器（免 CLI、跨平台）。不依赖 skillhub CLI，也不依赖外部 web_tool，直接通过 Python 标准库联网搜索 SkillHub 技能、查看分类、读取官方安全评测与压缩包结构，并将新技能安全安装到 Monolight 的 data/skills 目录。适用于用户想在 Monolight 中找技能、搜插件、预览安全报告、安装或更新 SkillHub 技能的场景。"
---

# Monolight 内置技能搜索与安装器（SkillHub Manual Installer）

本技能作为 Monolight 的内置技能，用于在不安装、不调用 `skillhub` CLI 且不依赖 `web_tool` 的情况下，直接通过 Python 3 标准库完成 SkillHub 技能的**在线搜索、分类浏览、安全预检、本地安装与已装列表查询**。

## 跨系统路径规范（重要）

本技能适用于 Windows、macOS 与 Linux 上的任意 Monolight 部署位置，禁止硬编码本机绝对路径，统一使用以下通用路径表达方式：

- **技能自身路径**：当前 `SKILL.md` 所在目录记为 `<SKILL_DIR>`，相对 Monolight 根目录为 `data/skills/skillhub-manual-installer`。
- **Monolight 技能根目录（安装目标）**：即 `<SKILL_DIR>/..`，对应 Monolight 目录树中的 `data/skills/`；若从 Monolight 会话临时工作目录（如 `temp/<session_id>`）相对定位，通常为 `../../data/skills`。
- **脚本默认自动定位**：`scripts/install_skill.py` 会通过自身所在位置向上两级（`Path(__file__).resolve().parents[2]`）自动定位 Monolight 的 `data/skills/` 目录；若脚本被单独拷贝到其他位置执行，则会从当前工作目录向上查找 `data/skills/`。也可通过 `--skills-dir` 或环境变量 `MONOLIGHT_SKILLS_DIR` 显式覆盖。
- **命令斜杠规范**：在文档与命令示例中使用正斜杠 `/` 连接路径（各主流系统的 Python 均原生兼容）。

## 一、搜索技能与浏览分类

当用户想查找某类能力（例如“有没有做 PPT 的技能”“搜一下表格处理技能”）时，直接运行搜索命令（无需 `web_tool`）：

```text
python "<SKILL_DIR>/scripts/install_skill.py" search "<关键词>" --limit 10
```

常用参数：

- `--category <key>`：按分类过滤，可先运行 `categories` 子命令查看全部分类。
- `--sort <field>`：排序字段，常用 `downloads`（下载量）、`stars`（收藏数）、`score`（综合评分）、`newest`（最新）。
- `--source <src>`：按来源筛选（如 `all`、`official`、`community`）。
- `--page <n>` / `--limit <n>`：分页参数，默认第 1 页、每页 10 条。

查看所有可用分类：

```text
python "<SKILL_DIR>/scripts/install_skill.py" categories
```

向用户展示搜索结果时，优先用简明列表呈现：技能名称、唯一标识（`@namespace/slug`）、简介、版本、下载量、是否需配置 API Key，方便用户挑选确认要安装哪一个。

## 二、安装前安全预检（必做）

在把任何新技能写入 Monolight 的 `data/skills/` 目录之前，必须先执行一次只读预检：

```text
python "<SKILL_DIR>/scripts/install_skill.py" inspect "<source>"
```

`<source>` 支持四种输入：

1. SkillHub 技能标识：`@namespace/slug` 或 `namespace/slug`（也支持仅输入 `slug`，脚本会自动检索匹配，若存在多个同名技能会列出候选供确认）。
2. SkillHub 网页链接：`https://www.skillhub.cn/skills/<namespace>/<slug>`。
3. 官方 Zip 下载直链：`https://api.skillhub.cn/api/v1/download?...`。
4. 本地 `.zip` 离线包相对或完整路径。

预检会在系统临时目录完成以下检查而不落盘到 `data/skills/`：

- 拉取官方详情与安全评测报告（科恩实验室、云鼎实验室），检查是否存在 `suspicious`（可疑/潜在风险）标记。
- 校验压缩包是否含路径穿越（Zip-Slip）、异常体积或非法字符。
- 校验包内是否存在合法的 `SKILL.md`（或 `skill.md`）及 `name` / `description` 元数据。
- 列出压缩包内所有文件与脚本清单（`.py`、`.sh`、`.mjs`、`.js`、`.ps1` 等）。

**风险拦截规则**：如果安全报告出现可疑标记（`has_suspicious_report: true`），或包内包含自更新/外传类脚本，必须先向用户如实说明具体风险项及报告链接，经用户明确同意后再执行安装。

## 三、安装到 Monolight 技能目录

用户确认安装后，直接运行：

```text
python "<SKILL_DIR>/scripts/install_skill.py" install "<source>"
```

脚本会自动定位 Monolight 的 `data/skills/<slug>` 作为安装目标；仅当需要安装到自定义路径时才附加 `--skills-dir "<目标目录>"`。

常用参数：

- `--slug <name>`：自定义安装后的子目录名（默认取 `SKILL.md` 中的 `name` 或解析出的 `slug`）。
- `--force`：当 `data/skills/<slug>` 已存在时覆盖更新（会先在暂存区完成新包校验，再备份旧目录后安全替换）。

## 四、查看已安装技能与最终核验

查看当前 Monolight `data/skills/` 下已安装的技能：

```text
python "<SKILL_DIR>/scripts/install_skill.py" list-installed
```

安装完成后，按以下顺序收尾：

1. 若宿主环境提供技能列表工具（如 `skill_tool`），刷新并检查新技能已出现在列表中且元数据有效。
2. 明确告知用户：已安装到 Monolight 技能目录 `data/skills/<slug>`、版本号、安全预检结果，以及未实际执行新技能内部脚本（如新技能依赖额外运行环境或 API Key，一并提示）。
