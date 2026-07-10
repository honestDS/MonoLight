import json
import re
from html.parser import HTMLParser

import markdown

from app.models.message import InternalMessage


class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.fed = []

    def handle_data(self, d):
        self.fed.append(d)

    def handle_starttag(self, tag, attrs):
        # 代码块需要在开始处补换行，防止与前文粘连
        if tag in ["pre"]:
            self.fed.append("\n")

    def handle_endtag(self, tag):
        # 针对块级元素添加换行，防止文字粘连，保留合理的段落结构
        if tag in ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "tr", "pre"]:
            self.fed.append("\n")

    def get_data(self):
        return "".join(self.fed)


def strip_tags(html: str) -> str:
    s = MLStripper()
    s.feed(html)
    return s.get_data()


def remove_markdown(text: str) -> str:
    """剔除文本中的 Markdown 标记，返回纯文本"""
    if not text:
        return text
    # 使用 Python-Markdown 内置 fenced_code 扩展识别 ```python 这类围栏代码块，
    # 避免代码块内容中的 # 注释被误识别为 Markdown 标题
    html = markdown.markdown(text, extensions=["fenced_code"])
    # 提取纯文本
    clean_text = strip_tags(html)
    # 清理多余的连续空白行（将 3 个以上的连续换行替换为 2 个换行，保持段落间距）
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
    return clean_text.strip()


def process_markdown_response(ai_msg: InternalMessage, enable_markdown: bool) -> InternalMessage:
    """
    在调度器保存消息前，处理 LLM 返回的 Message
    作为独立工具供调度器（Dispatcher）使用
    """
    # 存在工具调用，说明不是普通的纯文本回复，跳过转换
    if getattr(ai_msg, "tool_calls", None):
        return ai_msg

    # 如果允许 Markdown 或内容为空，直接返回
    if enable_markdown or not ai_msg.content:
        return ai_msg

    # 文件发送结构需要保持协议格式，仅清洗其中的回复文本
    if isinstance(ai_msg.content, str):
        try:
            parsed = json.loads(ai_msg.content)
            if isinstance(parsed, dict) and parsed.get("type") == "assistant_files":
                parsed["text"] = remove_markdown(str(parsed.get("text") or ""))
                ai_msg.content = json.dumps(parsed, ensure_ascii=False)
                return ai_msg
        except Exception:
            pass
        ai_msg.content = remove_markdown(ai_msg.content)

    return ai_msg
