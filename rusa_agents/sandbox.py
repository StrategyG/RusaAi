"""Workspace-confined file and command gateway. No shell and no network by default."""
from __future__ import annotations
import asyncio
import os
import shlex
from pathlib import Path
from typing import Sequence

class ToolDenied(PermissionError):
    """Raised when a tool request violates workspace policy."""

class WorkspaceGateway:
    DEFAULT_COMMANDS=frozenset({"python -m pytest","pytest","python -m compileall","ruff check","ruff format --check"})
    def __init__(self, workspace: str|Path, *, allowed_commands=None, command_timeout=120, max_output_bytes=100000):
        self.workspace=Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.allowed_commands=frozenset(self.DEFAULT_COMMANDS if allowed_commands is None else allowed_commands)
        self.command_timeout=max(1,min(command_timeout,600))
        self.max_output_bytes=max(1024,min(max_output_bytes,1000000))
    def _path(self, relative_path: str, *, for_write=False):
        candidate=Path(relative_path)
        if candidate.is_absolute() or not relative_path or ".." in candidate.parts:
            raise ToolDenied("path must be relative and cannot traverse")
        resolved=(self.workspace/candidate).resolve(strict=not for_write)
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolDenied("path escapes assigned workspace") from exc
        if for_write and resolved.exists() and resolved.is_symlink():
            raise ToolDenied("writing through symlinks is prohibited")
        return resolved
    def read_text(self,path: str,max_bytes=200000):
        target=self._path(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        data=target.read_bytes()
        if len(data)>min(max_bytes,1000000):
            raise ToolDenied("file exceeds read limit")
        return data.decode("utf-8")
    def write_text(self,path: str,content: str):
        target=self._path(path,for_write=True)
        if len(content.encode("utf-8"))>1000000:
            raise ToolDenied("file exceeds write limit")
        target.parent.mkdir(parents=True,exist_ok=True)
        target=self._path(path,for_write=True)
        target.write_text(content,encoding="utf-8")
    def list_files(self,path=".",limit=500):
        target=self.workspace if path=="." else self._path(path)
        if not target.is_dir():
            raise NotADirectoryError(path)
        result=[]
        for item in sorted(target.rglob("*")):
            if len(result)>=min(limit,2000):
                break
            if any(part in {".git",".venv","venv","__pycache__","node_modules"} for part in item.parts):
                continue
            if item.is_file() and not item.is_symlink():
                result.append(item.relative_to(self.workspace).as_posix())
        return result
    async def run_command(self,command: Sequence[str]):
        if not command or len(command)>32 or any(not isinstance(x,str) or not x for x in command):
            raise ToolDenied("invalid command arguments")
        if any(any(ch in arg for ch in (";","|","&",">","<","$","\n","\r")) for arg in command):
            raise ToolDenied("shell metacharacters are prohibited")
        prefix=" ".join(command[:3])
        normalized=" ".join(shlex.quote(part) for part in command)
        if prefix not in self.allowed_commands and normalized not in self.allowed_commands:
            raise ToolDenied(f"command is not allowlisted: {prefix}")
        env={"PATH":os.environ.get("PATH","/usr/bin:/bin"),"HOME":str(self.workspace),"PYTHONDONTWRITEBYTECODE":"1","PYTHONUNBUFFERED":"1"}
        process=await asyncio.create_subprocess_exec(*command,cwd=self.workspace,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,start_new_session=True)
        try:
            output,_=await asyncio.wait_for(process.communicate(),timeout=self.command_timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"exit_code":-1,"timed_out":True,"output":"Command timed out"}
        decoded=output[:self.max_output_bytes].decode("utf-8",errors="replace")
        if len(output)>self.max_output_bytes:
            decoded+="\n[output truncated]"
        return {"exit_code":process.returncode,"timed_out":False,"output":decoded}
