"""
LLM-based keyword extraction for knowledge graph.

Supports:
- OpenAI API (GPT-4, GPT-3.5-turbo)
- Claude API (Claude-3, Claude-2)
- DeepSeek API (兼容 OpenAI 格式)
- Fallback to jieba if LLM unavailable
"""

import json
import logging
import os
import re
from typing import Optional

import httpx

from memory_gateway.config import log

# LLM 配置
LLM_PROVIDER = os.environ.get("MEMORY_LLM_PROVIDER", "openai")  # openai, claude
LLM_API_KEY = os.environ.get("MEMORY_LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("MEMORY_LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("MEMORY_LLM_MODEL", "")
LLM_TIMEOUT = int(os.environ.get("MEMORY_LLM_TIMEOUT", "10"))  # seconds

# 默认模型
_DEFAULT_MODELS = {
    "openai": "gpt-3.5-turbo",
    "claude": "claude-3-haiku-20240307",
}

# Prompt for keyword extraction
_KEYWORD_EXTRACTION_PROMPT = """Extract key terms from the following text for a knowledge graph.

Rules:
1. Extract noun phrases, technical terms, proper nouns, and important concepts
2. Keep multi-word terms together (e.g., "Memory Gateway", "低空目标")
3. Return up to 15 terms
4. Return as JSON array of strings
5. Preserve original language (Chinese/English)

Text:
{text}

Return ONLY a JSON array of strings, no other text."""


def extract_keywords_with_llm_sync(content: str) -> Optional[list[str]]:
    """Extract keywords using LLM API (同步版本).
    
    Returns:
        List of keywords, or None if LLM unavailable
    """
    if not LLM_API_KEY:
        log.debug("No LLM API key configured, skipping LLM extraction")
        return None
    
    provider = LLM_PROVIDER.lower()
    model = LLM_MODEL or _DEFAULT_MODELS.get(provider, "gpt-3.5-turbo")
    
    try:
        if provider == "openai":
            return _extract_with_openai_sync(content, model)
        elif provider == "claude":
            return _extract_with_claude_sync(content, model)
        else:
            log.warning(f"Unknown LLM provider: {provider}")
            return None
    except Exception as e:
        log.warning(f"LLM extraction failed: {e}")
        return None


def _extract_with_openai_sync(content: str, model: str) -> Optional[list[str]]:
    """Extract keywords using OpenAI API (同步).
    
    支持 DeepSeek 等兼容 OpenAI 格式的 API。
    DeepSeek 的 reasoning 模型会在 reasoning_content 字段返回推理过程。
    """
    base_url = LLM_BASE_URL or "https://api.openai.com/v1"
    url = f"{base_url}/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a keyword extraction assistant. Return ONLY a JSON array of strings."},
            {"role": "user", "content": _KEYWORD_EXTRACTION_PROMPT.format(text=content)},
        ],
        "temperature": 0.1,
        "max_tokens": 500,  # DeepSeek reasoning 需要更多 tokens
    }
    
    with httpx.Client(timeout=LLM_TIMEOUT) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        
        # 优先使用 content，如果为空则尝试 reasoning_content（DeepSeek 特有）
        text = message.get("content", "").strip()
        if not text:
            text = message.get("reasoning_content", "").strip()
            if text:
                log.debug("Using reasoning_content from DeepSeek model")
        
        if not text:
            log.warning("LLM returned empty content and reasoning_content")
            return None
        
        # Parse JSON array
        return _parse_json_array(text)


def _extract_with_claude_sync(content: str, model: str) -> Optional[list[str]]:
    """Extract keywords using Claude API (同步)."""
    base_url = LLM_BASE_URL or "https://api.anthropic.com"
    url = f"{base_url}/v1/messages"
    
    headers = {
        "x-api-key": LLM_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": model,
        "max_tokens": 200,
        "messages": [
            {"role": "user", "content": _KEYWORD_EXTRACTION_PROMPT.format(text=content)},
        ],
    }
    
    with httpx.Client(timeout=LLM_TIMEOUT) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        text = data["content"][0]["text"].strip()
        
        # Parse JSON array
        return _parse_json_array(text)


def _parse_json_array(text: str) -> Optional[list[str]]:
    """Parse JSON array from LLM response."""
    # Try to find JSON array in the response
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if not match:
        log.warning(f"Failed to find JSON array in LLM response: {text[:100]}")
        return None
    
    try:
        terms = json.loads(match.group())
        if isinstance(terms, list):
            # Filter and clean terms
            cleaned = []
            for term in terms:
                if isinstance(term, str) and len(term) >= 2:
                    cleaned.append(term.strip())
            return cleaned[:15]
    except json.JSONDecodeError as e:
        log.warning(f"Failed to parse JSON array: {e}")
    
    return None


# Cache for LLM results (simple in-memory cache)
_llm_cache: dict[str, list[str]] = {}
_CACHE_MAX_SIZE = 1000


def _get_cache_key(content: str) -> str:
    """Generate cache key for content."""
    # Use first 100 chars as cache key
    return content[:100]


def get_cached_keywords(content: str) -> Optional[list[str]]:
    """Get cached keywords for content."""
    key = _get_cache_key(content)
    return _llm_cache.get(key)


def cache_keywords(content: str, keywords: list[str]) -> None:
    """Cache keywords for content."""
    global _llm_cache
    
    # Simple cache eviction
    if len(_llm_cache) >= _CACHE_MAX_SIZE:
        # Remove oldest entries
        keys_to_remove = list(_llm_cache.keys())[:_CACHE_MAX_SIZE // 2]
        for key in keys_to_remove:
            del _llm_cache[key]
    
    key = _get_cache_key(content)
    _llm_cache[key] = keywords
