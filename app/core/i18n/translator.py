import importlib
import pkgutil
from typing import Any

from app.core.i18n import locales
from app.core.i18n.context import get_current_locale, get_current_log_locale
from app.core.i18n.locale import DEFAULT_LOCALE, normalize_locale

# 内存中缓存所有翻译资源: { "zh": { "MSG_XYZ": "...", ... }, "en": { ... } }
_translations: dict[str, dict[str, str]] = {}


def _load_translations() -> None:
    """自动扫描 locales 目录，合并所有命名空间的翻译"""
    # 查找受支持的语言目录
    for _, lang_module_name, is_pkg in pkgutil.iter_modules(locales.__path__):
        if not is_pkg:
            continue

        lang = lang_module_name
        if lang not in _translations:
            _translations[lang] = {}

        # 导入该语言包
        lang_pkg = importlib.import_module(f"app.core.i18n.locales.{lang}")

        # 遍历该语言包下的所有模块（命名空间）
        for _, ns_module_name, _ in pkgutil.iter_modules(lang_pkg.__path__):
            ns_module = importlib.import_module(f"app.core.i18n.locales.{lang}.{ns_module_name}")
            if hasattr(ns_module, "MESSAGES") and isinstance(ns_module.MESSAGES, dict):
                # 将该模块中的 MESSAGES 合并到当前语言包
                _translations[lang].update(ns_module.MESSAGES)


# 模块导入时立刻执行加载
_load_translations()


def _is_log_key(key: str) -> bool:
    return key.startswith("LOG_") or key.startswith("MSG_LOG_") or key.startswith("ERR_LOG_")


def t(key: str, locale: str | None = None, default: str | None = None, **params: Any) -> str:
    """
    翻译函数
    :param key: 文案标识（如 "ERR_USER_NOT_FOUND"）
    :param locale: 显式指定语言；为 None 时取 get_current_locale()
    :param default: 当翻译缺失时使用的默认值（如果未提供则回退到 key 本身）
    :param params: 占位符参数，用于 str.format 填充
    :return: 翻译后的文案
    """
    target_locale = locale or (get_current_log_locale() if _is_log_key(key) else None) or get_current_locale()

    # 1. 尝试从目标语言获取
    lang_dict = _translations.get(target_locale)
    text = lang_dict.get(key) if lang_dict else None

    # 2. 如果获取不到，回退到默认语言
    if text is None:
        default_dict = _translations.get(DEFAULT_LOCALE)
        text = default_dict.get(key) if default_dict else None

    # 3. 如果默认语言也没有，回退到 default 或 key 本身
    if text is None:
        text = default if default is not None else key

    # 处理占位符
    if params:
        try:
            return text.format(**params)
        except KeyError:
            # 如果参数不匹配，返回原始文本
            return text

    return text


def message_platform_t(key: str, language: str | None, default: str | None = None, **params: Any) -> str:
    """Translate message-platform content using only the platform's configured language."""
    platform_locale = language if isinstance(language, str) else None
    return t(key, locale=normalize_locale(platform_locale), default=default, **params)
