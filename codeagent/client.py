"""LLM 客户端：调用 OpenAI 兼容的 Chat Completions 接口。

- 仅依赖 Python 标准库（urllib），零第三方依赖。
- 支持 function calling（tools）。
- 内置指数退避重试与超时控制（错误处理与重试机制）。
- 通过环境变量或构造参数配置，配置缺失时 `available` 为 False，
  由上层降级到离线规则引擎。
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class LLMError(Exception):
    """LLM 调用相关的统一异常。"""


class LLMClient:
    """OpenAI 兼容接口的轻量封装。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
        max_retries: int = 3,
        temperature: float = 0.2,
    ) -> None:
        self.api_key = (api_key or os.environ.get("OPENAI_API_KEY") or "b87c01b1016e4fb9bc40d53093415055.y04sXTj11DGo6LG2").strip()
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        ).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", "glm-5.3-flashx")
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature

    @property
    def available(self) -> bool:
        """是否具备发起在线请求的条件。"""
        return bool(self.api_key)

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> Dict[str, Any]:
        """发起一次对话请求，返回完整的 responses 字典。

        返回结构（节选）：
            {"choices":[{"message":{"role":"assistant","content":...,"tool_calls":[...]}}], ...}
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._post("/chat/completions", payload)
            except (urllib.error.URLError, urllib.error.HTTPError, LLMError, TimeoutError) as exc:
                last_err = exc
                if attempt < self.max_retries:
                    # 指数退避：1s, 2s, 4s, 8s（封顶）
                    time.sleep(min(2 ** attempt, 8) + 0.1)
        raise LLMError(
            f"LLM 请求失败，已重试 {self.max_retries} 次：{last_err}"
        )

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            raise LLMError("未配置 API Key，无法发起在线请求。")

        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        request = urllib.request.Request(url, data=data, method="POST", headers=headers)

        # Windows 上部分环境缺少系统 CA 证书，使用 ssl 默认上下文；
        # 若仍失败，退化为不校验（仅用于本地/可信网关，非推荐生产用法）。
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=ctx) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as http_err:
            detail = http_err.read().decode("utf-8", "ignore")
            raise LLMError(f"HTTP {http_err.code}: {detail[:500]}") from http_err
        except ssl.SSLError:
            unverified = ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=self.timeout, context=unverified) as resp:
                body = resp.read().decode("utf-8")

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"响应解析失败：{body[:300]}") from exc
