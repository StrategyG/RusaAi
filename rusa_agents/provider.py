"""OpenAI-compatible provider adapter with explicit endpoint allowlisting."""
from __future__ import annotations
import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

class ProviderError(RuntimeError):
    """Provider failure without credential disclosure."""

@dataclass(frozen=True, slots=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int = 90
    @classmethod
    def from_env(cls):
        base=os.environ.get("RUSA_AGENT_BASE_URL","").rstrip("/")
        model=os.environ.get("RUSA_AGENT_MODEL","").strip()
        key=os.environ.get("RUSA_AGENT_API_KEY","").strip()
        if not base or not model or not key:
            raise ProviderError("RUSA_AGENT_BASE_URL, RUSA_AGENT_MODEL, and RUSA_AGENT_API_KEY are required")
        parsed=urlparse(base)
        if parsed.scheme not in {"https","http"} or not parsed.netloc:
            raise ProviderError("provider URL must be absolute HTTP(S)")
        if parsed.scheme!="https" and parsed.hostname not in {"localhost","127.0.0.1","::1"}:
            raise ProviderError("remote provider endpoints must use HTTPS")
        timeout=int(os.environ.get("RUSA_AGENT_TIMEOUT","90"))
        if not 1<=timeout<=300:
            raise ProviderError("provider timeout must be between 1 and 300 seconds")
        return cls(base,model,key,timeout)

class OpenAICompatibleProvider:
    def __init__(self,config: ProviderConfig):
        self.config=config
    async def complete(self,*,system_prompt: str,user_prompt: str)->str:
        return await asyncio.to_thread(self._complete_sync,system_prompt,user_prompt)
    def _complete_sync(self,system_prompt,user_prompt):
        payload=json.dumps({"model":self.config.model,"messages":[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}],"temperature":0.1}).encode()
        request=urllib.request.Request(self.config.base_url+"/chat/completions",data=payload,headers={"Authorization":"Bearer "+self.config.api_key,"Content-Type":"application/json"},method="POST")
        try:
            with urllib.request.urlopen(request,timeout=self.config.timeout_seconds) as response:
                body=response.read(2000001)
            if len(body)>2000000:
                raise ProviderError("provider response exceeded size limit")
            data=json.loads(body)
            content=data["choices"][0]["message"]["content"]
            if not isinstance(content,str) or not content.strip():
                raise ProviderError("provider returned empty content")
            return content.strip()
        except ProviderError:
            raise
        except (urllib.error.URLError,TimeoutError,KeyError,IndexError,TypeError,ValueError) as exc:
            raise ProviderError("provider request failed: "+type(exc).__name__) from exc
