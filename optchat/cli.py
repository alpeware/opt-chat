"""Command-line interface for OptChat (§10).

Provides:
  - Interactive chat session with live streaming, view display, and plain scrolling.
  - HTML browser export (/browse command).
  - Historical messages importer (/import).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Optional

from rich.console import Console

from optchat.agent import TurnAgent
from optchat.compactor import Compactor
from optchat.constants import VIEW
from optchat.providers import AgyProvider, AnthropicProvider, BaseLLMProvider, MockLLMProvider, OpenAIProvider
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file

console = Console()


def create_provider(
    provider_name: str,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    compact_model: Optional[str] = None,
    timeout: float = 300.0,
) -> BaseLLMProvider:
    p = provider_name.lower().strip()
    if p == "agy":
        return AgyProvider(
            model=model or "gemini-3.8-flash-medium",
            compact_model=compact_model or "gemini-3.8-flash-low",
            timeout=timeout,
        )
    elif p == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model or "claude-3-7-sonnet-latest", timeout=timeout)
    elif p in ("openai", "openrouter"):
        return OpenAIProvider(api_key=api_key, model=model or "gpt-4o", timeout=timeout)
    elif p == "mock":
        return MockLLMProvider()
    else:
        raise ValueError(f"Unknown provider: {provider_name}. Choose agy, anthropic, openai, or mock.")


async def run_chat_session(
    chat_dir: Path,
    provider_name: str = "mock",
    model: Optional[str] = None,
    compactor_provider_name: Optional[str] = None,
    compactor_model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 300.0,
) -> None:
    storage = Storage(chat_dir)
    storage.open()

    view = LiveView(storage)
    view.rebuild()

    main_provider = create_provider(provider_name, model, api_key, timeout=timeout)
    compactor_pname = compactor_provider_name or provider_name
    comp_provider = (
        main_provider
        if compactor_pname == provider_name and compactor_model is None
        else create_provider(compactor_pname, compactor_model, api_key)
    )

    compactor = Compactor(storage, view, comp_provider)
    compactor.start()

    tool_registry = ToolRegistry()

    def ui_handler(event_type: str, content: str) -> None:
        if event_type == "text":
            console.print(content, end="")
        elif event_type == "reasoning":
            console.print(f"\n[dim italic][Thinking: {content.strip()}][/dim italic]\n")
        elif event_type == "tool_call":
            console.print(f"\n[cyan bold]➜ Tool Call:[/cyan bold] [cyan]{content}[/cyan]")
        elif event_type == "log_tool":
            pass
        elif event_type == "log_echo":
            preview = content[:200] + ("..." if len(content) > 200 else "")
            console.print(f"[yellow bold]➜ Result:[/yellow bold] [yellow]{preview}[/yellow]\n")
        elif event_type == "settling":
            console.print(f"[dim italic]{content}[/dim italic]")
        elif event_type == "subagent_spawn":
            console.print(f"\n[magenta bold]➜ Subagents:[/magenta bold] [magenta]{content}[/magenta]")
        elif event_type == "subagent_complete":
            console.print(f"\n[magenta bold]➜ Subagents Done:[/magenta bold] [dim magenta]{content}[/dim magenta]")
        elif event_type == "subagent_report":
            console.print(f"\n[green bold]➜ Subagent Report:[/green bold] [green]{content}[/green]\n")
        elif event_type == "log_talk":
            console.print()

    agents_md = chat_dir.parent / "AGENTS.md"
    agent = TurnAgent(
        storage=storage,
        view=view,
        compactor=compactor,
        provider=main_provider,
        tool_registry=tool_registry,
        agents_md_path=agents_md if agents_md.exists() else None,
        git_auto_commit=True,
        ui_callback=ui_handler,
    )

    console.print("\n[bold green]================ OptChat Started ================[/bold green]")
    console.print(f"Chat directory: [cyan]{storage.chat_dir}[/cyan]")
    console.print(f"Messages in log: [cyan]{len(storage.messages)}[/cyan]")
    console.print(f"Tree nodes built: [cyan]{len(storage.tree)}[/cyan]")

    # Print current view on start (§10)
    console.print("\n[bold]Current View:[/bold]")
    console.print(f"[dim]{view.render()}[/dim]\n")
    console.print("[dim]Commands: /view, /stats, /browse, /exit[/dim]\n")
    try:
        export_html_to_file(storage, view, storage.chat_dir / "browse.html")
    except Exception:
        pass

    loop = asyncio.get_running_loop()

    try:
        while True:
            try:
                # Read user input asynchronously from stdin
                user_input = await loop.run_in_executor(None, sys.stdin.readline)
                if not user_input:
                    break
                line = user_input.strip()
                if not line:
                    continue

                if line in ("/exit", "/quit"):
                    console.print("[yellow]Exiting OptChat.[/yellow]")
                    break
                elif line == "/view":
                    console.print("\n[bold]Current Live View:[/bold]")
                    console.print(view.render())
                    console.print()
                    continue
                elif line == "/stats":
                    total_bytes = view.compute_size()
                    pct = round((total_bytes / VIEW) * 100, 1)
                    console.print(f"Messages: {len(storage.messages)}")
                    console.print(f"Tree nodes: {len(storage.tree)}")
                    console.print(f"View size: {total_bytes} / {VIEW} bytes ({pct}%)")
                    console.print(f"View settled: {view.is_settled()}")
                    continue
                elif line == "/browse":
                    html_file = storage.chat_dir / "browse.html"
                    export_html_to_file(storage, view, html_file)
                    console.print(f"[green]HTML report generated at: {html_file}[/green]")
                    continue

                # Normal turn
                console.print(f"[bold blue]You:[/bold blue] {line}")
                console.print("[bold green]OptChat:[/bold green] ", end="")
                await agent.submit_user_message(line)
                # Wait for agent turn to finish
                while agent.is_running_turn:
                    await asyncio.sleep(0.05)

            except (KeyboardInterrupt, asyncio.CancelledError):
                console.print("\n[red]Turn interrupted by user.[/red]")
                agent.cancel_turn()
            except Exception as e:
                console.print(f"\n[bold red]Turn error:[/bold red] [red]{e}[/red]\n")
                agent.cancel_turn()

    finally:
        compactor.stop()
        storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="OptChat: an endless chat where the AI remembers everything")
    subparsers = parser.add_subparsers(dest="subcommand")

    default_chat_dir = os.path.expanduser(os.environ.get("OPTCHAT_DIR", "~/.optchat"))

    # chat command
    chat_parser = subparsers.add_parser("chat", help="Start an interactive chat session")
    chat_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")
    chat_parser.add_argument("--provider", default="agy", choices=["agy", "mock", "anthropic", "openai"], help="LLM Provider (default: agy)")
    chat_parser.add_argument("--model", default=None, help="Model name (e.g. gemini-3.8-flash-high, claude-sonnet-5-5-medium)")
    chat_parser.add_argument("--compactor-provider", default=None, help="Compactor LLM Provider (default: same as provider)")
    chat_parser.add_argument("--compactor-model", default=None, help="Compactor model name (e.g. gemini-3.8-flash-high)")
    chat_parser.add_argument("--timeout", type=float, default=300.0, help="Per-turn timeout in seconds (default: 300.0)")

    # mcp command
    mcp_parser = subparsers.add_parser("mcp", help="Run OptChat Model Context Protocol (MCP) stdio server for agy")
    mcp_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")

    # browse command
    browse_parser = subparsers.add_parser("browse", help="Generate an HTML memory report")
    browse_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")
    browse_parser.add_argument("--output", default=None, help="Output HTML file path (default: <chat-dir>/browse.html)")

    # import command
    import_parser = subparsers.add_parser("import", help="Import external history or notes")
    import_parser.add_argument("file", help="Path to input text or JSONL file")
    import_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")
    import_parser.add_argument("--kind", default="note", choices=["note", "user", "talk"], help="Message kind")

    # import-agy command
    agy_import_parser = subparsers.add_parser("import-agy", help="Import an agy transcript.jsonl into OptChat memory")
    agy_import_parser.add_argument("transcript", help="Path to transcript.jsonl or conversation ID")
    agy_import_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")
    agy_import_parser.add_argument("--skip-tools", action="store_true", help="Import only user prompts and agent talk (skipping tool noise)")

    # import-bulk command
    bulk_import_parser = subparsers.add_parser("import-bulk", help="Bulk import historical agy chats into OptChat")
    bulk_import_parser.add_argument("--workspace", default=None, help="Filter conversations by workspace path or substring (e.g. kaggle/gemma-4-developer-agent)")
    bulk_import_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")
    bulk_import_parser.add_argument("--include-tools", action="store_true", help="Include tool calls and outputs (defaults to clean dialogue Option B)")
    bulk_import_parser.add_argument("--compact", action="store_true", help="Run compactor to build summary tree nodes after import")
    bulk_import_parser.add_argument("--browse", action="store_true", help="Generate browse.html after import")
    bulk_import_parser.add_argument("--force", action="store_true", help="Ignore import manifest and force re-import")
    bulk_import_parser.add_argument("--dry-run", action="store_true", help="Scan and preview what would be imported without writing")

    args = parser.parse_args()

    if args.subcommand == "mcp":
        from optchat.mcp_server import OptChatMCPServer
        server = OptChatMCPServer(Path(args.chat_dir))
        asyncio.run(server.run_stdio())

    elif args.subcommand == "browse":
        storage = Storage(Path(args.chat_dir))
        storage.open()
        view = LiveView(storage)
        view.rebuild()
        target_out = Path(args.output) if args.output else storage.chat_dir / "browse.html"
        out_path = export_html_to_file(storage, view, target_out)
        console.print(f"[green]HTML report exported to {out_path}[/green]")
        storage.close()

    elif args.subcommand == "import":
        storage = Storage(Path(args.chat_dir))
        storage.open()
        input_path = Path(args.file)
        count = 0
        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    if isinstance(data, dict) and "text" in data:
                        storage.append_message(data.get("kind", args.kind), data["text"])
                    else:
                        storage.append_message(args.kind, stripped)
                except Exception:
                    storage.append_message(args.kind, stripped)
                count += 1
        console.print(f"[green]Imported {count} messages as kind '{args.kind}'.[/green]")
        storage.close()

    elif args.subcommand == "import-agy":
        from optchat.importer import import_agy_transcript
        storage = Storage(Path(args.chat_dir))
        storage.open()
        view = LiveView(storage)
        view.rebuild()

        t_path = Path(args.transcript)
        if not t_path.is_file():
            candidate = Path.home() / ".gemini" / "antigravity-cli" / "brain" / args.transcript / ".system_generated" / "logs" / "transcript.jsonl"
            if candidate.is_file():
                t_path = candidate
            else:
                console.print(f"[red]Could not find transcript at {args.transcript} or {candidate}[/red]")
                storage.close()
                return

        imported = import_agy_transcript(t_path, storage, view=view, skip_tool_noise=args.skip_tools)
        console.print(f"[green]Successfully imported {imported} messages from agy transcript into {storage.chat_dir}![/green]")
        storage.close()

    elif args.subcommand == "import-bulk":
        from optchat.bulk_importer import BulkImporter, run_bulk_compaction
        chat_dir = Path(args.chat_dir)
        storage = Storage(chat_dir)
        storage.open()
        view = LiveView(storage)
        view.rebuild()

        importer = BulkImporter(
            storage=storage,
            workspace_filter=args.workspace,
            skip_tools=not args.include_tools,
            force=args.force,
        )

        c_cnt, m_cnt = importer.import_all(dry_run=args.dry_run)
        if not args.dry_run:
            if m_cnt > 0:
                view.rebuild()
                console.print(f"[bold green]Rebuilt live view. Current messages: {len(storage.messages)} | Tree nodes: {len(storage.tree)}[/bold green]")

            if args.compact:
                asyncio.run(
                    run_bulk_compaction(
                        storage=storage,
                        view=view,
                        provider_name=getattr(args, "provider", "agy"),
                        model=getattr(args, "model", None),
                    )
                )

            if args.browse:
                target_out = storage.chat_dir / "browse.html"
                out_path = export_html_to_file(storage, view, target_out)
                console.print(f"[green]Memory report saved to {out_path}[/green]")

        storage.close()

    else:
        # Default to chat
        asyncio.run(
            run_chat_session(
                chat_dir=Path(getattr(args, "chat_dir", default_chat_dir)),
                provider_name=getattr(args, "provider", "agy"),
                model=getattr(args, "model", None),
                compactor_provider_name=getattr(args, "compactor_provider", None),
                compactor_model=getattr(args, "compactor_model", None),
                timeout=getattr(args, "timeout", 300.0),
            )
        )


if __name__ == "__main__":
    main()
