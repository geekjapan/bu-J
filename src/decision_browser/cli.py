from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

from decision_browser.audit import Journal, PersistenceError
from decision_browser.contracts import Budget, TaskSpec
from decision_browser.controller import Controller
from decision_browser.drivers.mock import MockBrowserDriver
from decision_browser.providers.azure import AzureDecisionProvider, validate_endpoint
from decision_browser.providers.mock import MockDecisionProvider
from decision_browser._runtime import azure_cli_executable


def azure_provider(args: argparse.Namespace) -> AzureDecisionProvider:
    endpoint = args.azure_endpoint or os.environ.get('AZURE_ENDPOINT', '')
    validate_endpoint(endpoint)
    deployment = args.azure_deployment or os.environ.get('AZURE_DEPLOYMENT_NAME', 'decision-1')
    if args.azure_auth == 'azure-cli':
        try:
            token = subprocess.run(
                [azure_cli_executable(), 'account', 'get-access-token', '--resource', 'https://cognitiveservices.azure.com/',
                 '--query', 'accessToken', '--output', 'tsv'],
                check=True, capture_output=True, text=True, encoding='utf-8', timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Azure CLI authentication failed') from None
        return AzureDecisionProvider(endpoint, deployment, entra_token=token)
    if args.azure_auth == 'entra':
        return AzureDecisionProvider(endpoint, deployment, entra_token=os.environ.get('AZURE_ENTRA_TOKEN'))
    return AzureDecisionProvider(endpoint, deployment, api_key=os.environ.get('AZURE_API_KEY'))


async def run(args: argparse.Namespace, task: TaskSpec, journal: Journal):
    routing = args.routing or ('model' if args.provider == 'azure' else 'rules')
    if args.provider == 'azure' and (routing != 'rules' or args.mode == 'shadow'):
        async with azure_provider(args) as provider:
            return await Controller(MockBrowserDriver(), provider, journal=journal, mode=args.mode,
                                    routing=routing, model=provider.MODEL).run(task)
    return await Controller(MockBrowserDriver(), MockDecisionProvider(), journal=journal,
                            mode=args.mode, routing=routing).run(task)


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Run synthetic fixture search with a mock or Azure decision provider')
    parser.add_argument('--query', default='')
    parser.add_argument('--mode', choices=('observe-only', 'shadow', 'enforced'), default='shadow')
    parser.add_argument('--routing', choices=('rules', 'model', 'hybrid'))
    parser.add_argument('--provider', choices=('mock', 'azure'), default='mock')
    parser.add_argument('--azure-endpoint')
    parser.add_argument('--azure-deployment')
    parser.add_argument('--azure-auth', choices=('api-key', 'entra', 'azure-cli'), default='azure-cli')
    parser.add_argument('--max-cost-units', type=int, default=100, help='Maximum model tokens plus calls, not a monetary limit')
    parser.add_argument('--journal', type=Path, default=Path('decision-browser.sqlite3'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--audit-jsonl', type=Path)
    parser.add_argument('--task-id', default=None)
    args = parser.parse_args()
    journal = None
    try:
        task = TaskSpec(task_id=args.task_id or uuid.uuid4().hex, query=args.query,
                        budget=Budget(max_cost_units=args.max_cost_units))
        journal = Journal(args.journal)
        result = asyncio.run(run(args, task, journal))
        if args.audit_jsonl:
            journal.export_events(args.audit_jsonl)
        output = result.model_dump_json(indent=2) + '\n'
        if args.output:
            args.output.write_text(output, encoding='utf-8')
        print(output, end='')
        return 0 if result.state in ('SUCCEEDED', 'OBSERVED') else 1
    except ValueError:
        print('Invalid task or Azure configuration; check endpoint, deployment, authentication, and budget.', file=sys.stderr)
        return 1
    except (OSError, PersistenceError):
        print('Result or audit export failed; SQLite journal remains authoritative.', file=sys.stderr)
        return 1
    finally:
        if journal is not None:
            journal.close()
