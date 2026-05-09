#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional local Ollama integration for explaining candidates.

This is deliberately not used as the core prediction engine. The quantitative
ranking should come from rules/backtests/ML; the LLM is best used for readable
risk summaries and sanity checks.
"""

from __future__ import annotations

import json
from typing import Any

import requests


DEFAULT_MODEL = "qwen3.6:35b-a3b-q8_0"


def explain_candidates(candidates: list[dict[str, Any]], model: str = DEFAULT_MODEL, host: str = "http://localhost:11434") -> str:
    payload = {
        "model": model,
        "stream": False,
        "prompt": _build_prompt(candidates),
    }
    response = requests.post(f"{host.rstrip('/')}/api/generate", json=payload, timeout=120)
    response.raise_for_status()
    return response.json().get("response", "").strip()


def _build_prompt(candidates: list[dict[str, Any]]) -> str:
    data = json.dumps(candidates[:20], ensure_ascii=False, indent=2)
    return f"""你是一个严格的A股短线量化风控助手。

请基于下面候选股的结构化指标做解释，不要编造新闻、公告或不存在的数据。
输出要求：
1. 先说明这不是投资建议；
2. 按候选股逐条解释强项、弱项、尾盘风险；
3. 给出需要进一步验证的分钟级/盘口级数据；
4. 不要承诺涨停或收益。

候选股数据：
{data}
"""
