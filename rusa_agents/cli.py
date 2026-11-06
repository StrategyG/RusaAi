"""CLI for running AICode in a pre-created isolated checkout."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
from .contracts import CodingTask,RepositoryTarget
from .orchestrator import AICodeOrchestrator
from .provider import OpenAICompatibleProvider,ProviderConfig
from .sandbox import WorkspaceGateway

def main():
    parser=argparse.ArgumentParser(description="Run the RUSA-AI coding agent")
    parser.add_argument("--workspace",required=True)
    parser.add_argument("--repository",required=True)
    parser.add_argument("--base-branch",default="main")
    parser.add_argument("--work-branch",required=True)
    parser.add_argument("--objective",required=True)
    parser.add_argument("--context",default="")
    parser.add_argument("--criteria",action="append",required=True)
    args=parser.parse_args()
    workspace=Path(args.workspace).resolve(strict=True)
    task=CodingTask(args.objective,RepositoryTarget(args.repository,args.base_branch,args.work_branch),args.criteria,args.context)
    runtime=AICodeOrchestrator(OpenAICompatibleProvider(ProviderConfig.from_env()),WorkspaceGateway(workspace))
    record=asyncio.run(runtime.run(task))
    print(json.dumps({"task_id":task.task_id,"state":record.state.value,"results":[{"role":x.role.value,"summary":x.summary,"success":x.success,"artifacts":x.artifacts} for x in record.results],"errors":record.errors},ensure_ascii=False,indent=2))
    return 0 if record.state== "succeeded" else 1

if __name__=="__main__":
    raise SystemExit(main())
