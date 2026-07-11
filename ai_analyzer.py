"""
Multi-provider AI signal analyzer. User apne "AI" page se jitne chahe AI
providers ki API key daal sakta hai (Claude, Gemini, DeepSeek, Mistral,
Grok, Together, Fireworks, OpenRouter, Cerebras, Perplexity, HuggingFace,
Cohere, Qwen/Aliyun). Har naye signal pe, enabled providers ko 'priority'
order me try karte hain (fallback chain) - jo pehla successfully respond
kare, uska result use hota hai. Har attempt ke baad us provider ka
status ('ok'/'failed') aur last_error DB me update ho jata hai, taaki
AI page pe user ko turant dikhe kaun sa AI chal raha hai, kaun fail ho raha hai.
"""
import json
import httpx
from datetime import datetime
from sqlalchemy.orm import Session

from db import AiProviderKey, decrypt_text

# ---------------------------------------------------------------
# PROVIDER CATALOG - naya provider add karna ho to bas yahan ek
# entry add karo, baaki sab (UI list, call logic) automatic chalega.
#   kind: 'openai_compatible' | 'anthropic' | 'gemini' | 'cohere'
# ---------------------------------------------------------------
PROVIDER_CATALOG = {
    "claude":     {"label": "Claude (Anthropic)", "kind": "anthropic",
                   "base_url": "https://api.anthropic.com/v1/messages",
                   "default_model": "claude-3-5-haiku-20241022"},
    "gemini":     {"label": "Gemini (Google)", "kind": "gemini",
                   "base_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                   "default_model": "gemini-2.0-flash"},
    "grok":       {"label": "Grok (xAI)", "kind": "openai_compatible",
                   "base_url": "https://api.x.ai/v1/chat/completions",
                   "default_model": "grok-2-latest"},
    "deepseek":   {"label": "DeepSeek", "kind": "openai_compatible",
                   "base_url": "https://api.deepseek.com/chat/completions",
                   "default_model": "deepseek-chat"},
    "mistral":    {"label": "Mistral", "kind": "openai_compatible",
                   "base_url": "https://api.mistral.ai/v1/chat/completions",
                   "default_model": "mistral-small-latest"},
    "together":   {"label": "Together AI", "kind": "openai_compatible",
                   "base_url": "https://api.together.xyz/v1/chat/completions",
                   "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
    "fireworks":  {"label": "Fireworks AI", "kind": "openai_compatible",
                   "base_url": "https://api.fireworks.ai/inference/v1/chat/completions",
                   "default_model": "accounts/fireworks/models/llama-v3p1-8b-instruct"},
    "openrouter": {"label": "OpenRouter", "kind": "openai_compatible",
                   "base_url": "https://openrouter.ai/api/v1/chat/completions",
                   "default_model": "openai/gpt-4o-mini"},
    "cerebras":   {"label": "Cerebras", "kind": "openai_compatible",
                   "base_url": "https://api.cerebras.ai/v1/chat/completions",
                   "default_model": "llama3.1-8b"},
    "perplexity": {"label": "Perplexity", "kind": "openai_compatible",
                   "base_url": "https://api.perplexity.ai/chat/completions",
                   "default_model": "sonar"},
    "huggingface": {"label": "Hugging Face", "kind": "openai_compatible",
                    "base_url": "https://router.huggingface.co/v1/chat/completions",
                    "default_model": "meta-llama/Llama-3.1-8B-Instruct"},
    "cohere":     {"label": "Cohere", "kind": "cohere",
                   "base_url": "https://api.cohere.com/v2/chat",
                   "default_model": "command-r"},
    "qwen":       {"label": "Qwen (Aliyun)", "kind": "openai_compatible",
                   "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                   "default_model": "qwen-plus"},
}

PROMPT_TEMPLATE = """Tum ek experienced forex/crypto trading signal analyst ho. Neeche ek
trading signal diya gaya hai jo Telegram channel se aaya hai. Isko analyse karo aur
JSON format me jawab do, kuch aur text mat likho.

Signal text: {text}
Parsed data: pair={pair}, direction={direction}, entry={entry}, tp={tp}, sl={sl}

Sirf ye JSON return karo:
{{"confidence": <0-100 number>, "verdict": "<one line Hinglish verdict>", "risk_note": "<one line risk warning agar koi ho, warna empty string>"}}
"""

TEST_PROMPT = 'Reply with ONLY this exact JSON and nothing else: {"ok": true}'


def _extract_json(raw: str) -> dict:
    """AI kabhi kabhi ```json fences ya extra text ke saath jawab deta hai -
    usme se pehla valid JSON object nikal lete hain."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Response me JSON nahi mila")
    return json.loads(raw[start:end + 1])


async def _call_openai_compatible(base_url: str, api_key: str, model: str, prompt: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]


async def _call_anthropic(api_key: str, model: str, prompt: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        r.raise_for_status()
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []))


async def _call_gemini(base_url: str, api_key: str, model: str, prompt: str) -> str:
    url = base_url.format(model=model)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{url}?key={api_key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
            },
        )
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


async def _call_cohere(api_key: str, model: str, prompt: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.cohere.com/v2/chat",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        data = r.json()
        parts = data.get("message", {}).get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


async def _call_provider(provider: str, api_key: str, model: str | None, prompt: str) -> str:
    """Provider catalog ke 'kind' ke hisaab se sahi API format use karke call karta hai."""
    cfg = PROVIDER_CATALOG.get(provider)
    if not cfg:
        raise ValueError(f"Unknown provider: {provider}")
    model = model or cfg["default_model"]
    kind = cfg["kind"]
    if kind == "openai_compatible":
        return await _call_openai_compatible(cfg["base_url"], api_key, model, prompt)
    if kind == "anthropic":
        return await _call_anthropic(api_key, model, prompt)
    if kind == "gemini":
        return await _call_gemini(cfg["base_url"], api_key, model, prompt)
    if kind == "cohere":
        return await _call_cohere(api_key, model, prompt)
    raise ValueError(f"Unsupported provider kind: {kind}")


async def test_provider_key(provider: str, api_key: str, model: str | None) -> tuple[bool, str | None]:
    """AI page ke 'Test' button ke liye - ek chhota sa prompt bhejke check karta
    hai ki key/model sahi hai ya nahi. Returns (ok, error_message)."""
    try:
        raw = await _call_provider(provider, api_key, model, TEST_PROMPT)
        if not raw or not raw.strip():
            return False, "Khali response mila"
        return True, None
    except httpx.HTTPStatusError as e:
        body = e.response.text[:200] if e.response is not None else ""
        return False, f"HTTP {e.response.status_code if e.response is not None else '?'}: {body}"
    except Exception as e:
        return False, str(e)[:200]


async def analyze_signal(db: Session, user_id: int, text: str, parsed: dict) -> dict | None:
    """User ke saare enabled AI providers ko priority order me try karta hai
    (fallback chain). Pehla jo successfully valid JSON de, uska result use
    hota hai. Har provider ke DB row ka status/last_error/last_used_at
    update hota jaata hai - taaki AI page pe live pata chale kaun kaam
    kar raha hai. Koi bhi provider configured na ho ya sab fail ho jayein
    to None return hota hai (signal tracking phir bhi chalti rahegi)."""
    providers = (
        db.query(AiProviderKey)
        .filter(AiProviderKey.user_id == user_id, AiProviderKey.enabled == True)  # noqa: E712
        .order_by(AiProviderKey.priority.asc(), AiProviderKey.id.asc())
        .all()
    )
    if not providers:
        return None

    prompt = PROMPT_TEMPLATE.format(
        text=text[:500], pair=parsed.get("pair"), direction=parsed.get("direction"),
        entry=parsed.get("entry"), tp=parsed.get("tp"), sl=parsed.get("sl"),
    )

    for row in providers:
        try:
            api_key = decrypt_text(row.encrypted_api_key)
            raw = await _call_provider(row.provider, api_key, row.model, prompt)
            parsed_ai = _extract_json(raw)
            row.status = "ok"
            row.last_error = None
            row.last_used_at = datetime.utcnow()
            db.commit()
            return {
                "confidence": parsed_ai.get("confidence"),
                "verdict": parsed_ai.get("verdict", ""),
                "risk_note": parsed_ai.get("risk_note", ""),
                "provider": row.provider,
            }
        except Exception as e:
            row.status = "failed"
            row.last_error = str(e)[:300]
            row.last_used_at = datetime.utcnow()
            db.commit()
            continue  # agla provider try karo (fallback)

    return None  # sab providers fail ho gaye
