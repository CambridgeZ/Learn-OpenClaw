import json
from pathlib import Path
from typing import Any

from core.llm import call_llm


MEMORY_FILEPATH = Path(r".\chat_memory\session.jsonl")                                # 对话记忆文件存储路径（jsonl格式）
LONG_TERM_MEMORY_FILEPATH = Path(r".\chat_memory\MEMORY.md")                          # 长期记忆文件存储路径（md格式）
MAX_CONTEXT_LENGTH = 128_000                                                          # 大模型最大上下文窗口大小（按token计算）
COMPRESS_THRESHOLD = 0.9                                                              # 摘要压缩阈值（达到阈值后自动摘要压缩）
KEEP_MESSAGES_ON_COMPRESS = 4                                                         # 摘要压缩对话之后保留的最近消息条数
LONG_TERM_MEMORY_HEADER = "# 长期记忆：包括用户偏好、重要事件、运行环境等等\n\n"            # MEMORY.md文件的标题
MESSAGE_KEYS = {"role", "content", "tool_calls", "tool_call_id", "reasoning_content"} # message字典中可出现的所有key值


class Memory:
    """一份 jsonl 对话记录 + 一个长期记忆文件。"""

    def __init__(self):
        """初始化记忆文件，并把已有的 session.jsonl 读回内存。"""
        MEMORY_FILEPATH.parent.mkdir(parents=True, exist_ok=True)
        LONG_TERM_MEMORY_FILEPATH.parent.mkdir(parents=True, exist_ok=True)
        if not LONG_TERM_MEMORY_FILEPATH.exists():
            LONG_TERM_MEMORY_FILEPATH.write_text(LONG_TERM_MEMORY_HEADER, encoding="utf-8")

        self.messages: list[dict[str, Any]] = []

        if MEMORY_FILEPATH.exists():
            for line in MEMORY_FILEPATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    self.messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # 上次崩溃如果停在 tool 调用中间，就丢掉这轮未完成消息。
        need_rewrite = False
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue

            tail = self.messages[index + 1:]
            if tail and not all(item.get("role") == "tool" for item in tail):
                break

            start = index - 1 if index > 0 and self.messages[index - 1].get("role") == "user" else index
            del self.messages[start:]
            need_rewrite = True
            break

        if need_rewrite:
            with MEMORY_FILEPATH.open("w", encoding="utf-8") as f:
                for message in self.messages:
                    f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def add_message(self, message: dict[str, Any]):
        """添加一条 message，写入 session.jsonl，并在最终助手回复后按需压缩。"""
        total_tokens = message.get("usage", {}).get("total_tokens", 0)
        should_compress = total_tokens > 0 and not message.get("tool_calls")
        message = {key: value for key, value in message.items() if key in MESSAGE_KEYS}
        self.messages.append(message)
        with MEMORY_FILEPATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

        if should_compress:
            self.compress(total_tokens)

    def build_context(self, system_prompt: str = "") -> list[dict[str, Any]]:
        """组装传给 LLM 的 messages，必要时把长期记忆放进 system prompt。"""
        if not system_prompt:
            return list(self.messages)

        long_term_memory = LONG_TERM_MEMORY_FILEPATH.read_text(encoding="utf-8").strip()
        if long_term_memory == LONG_TERM_MEMORY_HEADER.strip():
            long_term_memory = ""

        system_message = {"role": "system", "content": system_prompt}
        if long_term_memory:
            system_message["content"] += f"\n\n长期记忆：\n{long_term_memory}"

        if self.messages and self.messages[0].get("role") == "system":
            system_message["content"] += f"\n\n{self.messages[0]['content']}"
            return [system_message, *self.messages[1:]]

        return [system_message, *self.messages]

    def compress(self, total_tokens: int):
        """当上下文接近上限时，把较早消息压缩成摘要，并保留最近几条消息。"""
        if total_tokens <= MAX_CONTEXT_LENGTH * COMPRESS_THRESHOLD:
            return
        if len(self.messages) <= KEEP_MESSAGES_ON_COMPRESS:
            return

        split_index = max(0, len(self.messages) - KEEP_MESSAGES_ON_COMPRESS)

        # 避免把 assistant tool_calls 和后续 tool 结果拆到摘要边界两边。
        while split_index > 0 and self.messages[split_index].get("role") == "tool":
            split_index -= 1

        if (
            split_index > 0
            and self.messages[split_index].get("role") == "assistant"
            and self.messages[split_index].get("tool_calls")
            and self.messages[split_index - 1].get("role") == "user"
        ):
            split_index -= 1

        old_messages = self.messages[:split_index]
        recent_messages = self.messages[split_index:]
        if not old_messages:
            return

        long_term_memory = LONG_TERM_MEMORY_FILEPATH.read_text(encoding="utf-8").strip()
        if long_term_memory == LONG_TERM_MEMORY_HEADER.strip():
            long_term_memory = "无"

        response = call_llm(messages=[
            *old_messages,
            {
                "role": "user",
                "content": (
                    f"已有长期记忆：\n{long_term_memory}\n\n请压缩以上对话历史，并判断是否有值得长期记住的信息（用户偏好、关键事实、运行环境等等。注意排除已有的长期记忆）。\n"
                    "只返回 JSON，不要使用 Markdown 代码块。"
                    "JSON 包含 summary(摘要总结) 和 memory_update(值得长期记忆的信息) 两个字符串字段。"
                ),
            },
        ])

        try:
            result = json.loads(response.get("content", ""))
            summary = result.get("summary", "")
            memory_update = result.get("memory_update", "")
        except json.JSONDecodeError:
            summary = response.get("content", "")
            memory_update = ""

        self.messages = [{"role": "system", "content": f"对话历史摘要：\n{summary}"}, *recent_messages]
        with MEMORY_FILEPATH.open("w", encoding="utf-8") as f:
            for message in self.messages:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")

        if memory_update:
            with LONG_TERM_MEMORY_FILEPATH.open("a", encoding="utf-8") as f:
                f.write("\n" + memory_update)
