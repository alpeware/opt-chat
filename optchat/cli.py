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
from optchat.providers import BaseLLMProvider, create_provider
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file

console = Console()


async def run_chat_session(
    chat_dir: Path,
    provider_name: str = "agy",
    model: Optional[str] = None,
    compactor_provider_name: Optional[str] = None,
    compactor_model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 300.0,
    workspace: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> None:
    storage = Storage(chat_dir)
    storage.open()

    view = LiveView(storage)
    view.rebuild()

    main_provider = create_provider(
        provider_name,
        model,
        api_key,
        timeout=timeout,
        workspace=workspace,
        conversation_id=conversation_id,
    )
    compactor_pname = compactor_provider_name or provider_name
    comp_provider = (
        main_provider
        if compactor_pname == provider_name and compactor_model is None
        else create_provider(compactor_pname, compactor_model, api_key, workspace=workspace)
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
    chat_parser.add_argument("--provider", default="agy", choices=["agy", "mock"], help="LLM Provider (default: agy)")
    chat_parser.add_argument("--model", default=None, help="Model name (e.g. gemini-3.8-flash-high)")
    chat_parser.add_argument("--compactor-provider", default=None, help="Compactor LLM Provider (default: same as provider)")
    chat_parser.add_argument("--compactor-model", default=None, help="Compactor model name (e.g. gemini-3.8-flash-low)")
    chat_parser.add_argument("--timeout", type=float, default=300.0, help="Per-turn timeout in seconds (default: 300.0)")
    chat_parser.add_argument("--workspace", default=None, help="Workspace path for agy execution (default: current directory)")
    chat_parser.add_argument("--conversation", default=None, help="Antigravity conversation ID to resume")

    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Run background OptChat memory daemon & IPC socket")
    daemon_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")
    daemon_parser.add_argument("--compactor-model", default=None, help="Compactor model name (e.g. gemini-3.8-flash-low)")
    daemon_parser.add_argument("--socket-path", default=None, help="Unix domain socket path (default: <chat-dir>/engine.sock)")
    daemon_parser.add_argument("--provider", default="agy", choices=["agy", "mock"], help="LLM Provider for compactor (default: agy)")
    daemon_parser.add_argument("--sandbox", default="none", choices=["auto", "bwrap", "proot", "unshare", "none"], help="Sandbox isolation for daemon and child agy invocations (default: none)")

    # exec command
    exec_parser = subparsers.add_parser("exec", help="Run a command (e.g. agy) inside the OptChat sandbox")
    exec_parser.add_argument("--sandbox", default="auto", choices=["auto", "bwrap", "proot", "unshare", "none"], help="Sandbox backend (default: auto)")
    exec_parser.add_argument("--workspace", default=None, help="Workspace directory to mount read-write (default: current directory)")
    exec_parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command and arguments to execute")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Synchronize memory idempotently with a remote peer over SSH")
    sync_parser.add_argument("peer", nargs="?", default=None, help="Remote SSH host or user@host (e.g. desktop, laptop, phone)")
    sync_parser.add_argument("--all", action="store_true", help="Sync with all configured peers in ~/.optchat/config.json")
    sync_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")
    sync_parser.add_argument("--remote-dir", default=None, help="Remote chat directory (default: ~/.optchat)")

    # sync-exchange command (internal stdio for SSH sync)
    sync_exchange_parser = subparsers.add_parser("sync-exchange", help="Internal SSH stdio handler for peer sync")
    sync_exchange_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")

    # hook command
    hook_parser = subparsers.add_parser("hook", help="Execute Antigravity lifecycle hook")
    hook_parser.add_argument("event", help="Lifecycle event name (pre-invocation, post-tool, post-invocation, stop)")

    # install-hooks command
    install_hooks_parser = subparsers.add_parser("install-hooks", help="Register OptChat memory bridge in Antigravity hooks.json")
    install_hooks_parser.add_argument("--workspace", default=None, help="Workspace path (default: global ~/.gemini/config/hooks.json)")

    # uninstall-hooks command
    uninstall_hooks_parser = subparsers.add_parser("uninstall-hooks", help="Remove OptChat memory bridge from hooks.json")
    uninstall_hooks_parser.add_argument("--workspace", default=None, help="Workspace path (default: global ~/.gemini/config/hooks.json)")

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

    # web command
    web_parser = subparsers.add_parser("web", help="Start the responsive mobile/desktop web interface")
    web_parser.add_argument("--host", default="127.0.0.1", help="Host interface to listen on (default: 127.0.0.1 for browser Secure Context voice dictation)")
    web_parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    web_parser.add_argument("--chat-dir", default=default_chat_dir, help=f"Path to chat directory (default: {default_chat_dir})")
    web_parser.add_argument("--provider", default="agy", choices=["agy", "mock"], help="LLM Provider (default: agy)")
    web_parser.add_argument("--model", default=None, help="Model name (e.g. gemini-3.8-flash-medium)")
    web_parser.add_argument("--compactor-model", default=None, help="Compactor model name (e.g. gemini-3.8-flash-low)")
    web_parser.add_argument("--workspace", default=None, help="Workspace directory for agy execution (default: opt-chat)")
    web_parser.add_argument("--conversation", default=None, help="Antigravity conversation ID to resume")

    # device command
    device_parser = subparsers.add_parser(
        "device",
        aliases=["device-name", "whoami"],
        help="Display or configure the local device name",
    )
    device_parser.add_argument("--set", dest="set_name", default=None, help="Set persistent device name in ~/.optchat/config.json")
    device_parser.add_argument("--verbose", "-v", action="store_true", help="Display resolution source and configuration details")

    args = parser.parse_args()

    if args.subcommand == "mcp":
        from optchat.mcp_server import OptChatMCPServer
        server = OptChatMCPServer(Path(args.chat_dir))
        asyncio.run(server.run_stdio())

    elif args.subcommand == "browse":
        from optchat.engine_client import EngineClient
        chat_dir = Path(args.chat_dir)
        client = EngineClient(socket_path=chat_dir / "engine.sock")
        target_out = Path(args.output) if args.output else chat_dir / "browse.html"
        if client.is_daemon_alive():
            res = client.call("export_browse")
            console.print(f"[green]HTML report exported to {res.get('path', target_out)}[/green]")
        else:
            storage = Storage(chat_dir)
            storage.open()
            try:
                view = LiveView(storage)
                view.rebuild()
                out_path = export_html_to_file(storage, view, target_out)
                console.print(f"[green]HTML report exported to {out_path}[/green]")
            finally:
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

    elif args.subcommand == "daemon":
        from optchat.daemon import run_daemon
        socket_path = Path(args.socket_path) if args.socket_path else None
        asyncio.run(
            run_daemon(
                chat_dir=Path(args.chat_dir),
                compactor_model=args.compactor_model,
                socket_path=socket_path,
                provider_name=args.provider,
                sandbox=args.sandbox,
            )
        )

    elif args.subcommand == "exec":
        import subprocess
        from optchat.sandbox import SandboxManager
        ws = Path(args.workspace) if args.workspace else Path.cwd()
        backend = SandboxManager.detect_backend(args.sandbox)
        config = SandboxManager.create_default_config(ws, backend=backend)
        cmd_to_run = list(args.cmd)
        if cmd_to_run and cmd_to_run[0] == "--":
            cmd_to_run = cmd_to_run[1:]
        if not cmd_to_run:
            console.print("[red]Error: No command specified to execute.[/red]")
            return
        wrapped = SandboxManager.wrap_command(cmd_to_run, config)
        env = SandboxManager.filter_env(config)
        ret = subprocess.call(wrapped, env={**os.environ, **env})
        sys.exit(ret)

    elif args.subcommand == "sync":
        from optchat.engine_client import EngineClient
        from optchat.sync import (
            sync_payload_over_ssh,
            export_sync_payload,
            apply_sync_payload,
            get_configured_peers,
            get_ssh_config_hosts,
        )
        chat_dir = Path(args.chat_dir)
        peers_to_sync: List[str] = []
        if args.peer:
            peers_to_sync = [args.peer]
        elif args.all or not args.peer:
            peers_to_sync = get_configured_peers(chat_dir)
            if not peers_to_sync:
                ssh_hosts = get_ssh_config_hosts()
                if ssh_hosts:
                    console.print(f"[yellow]No peer specified and no peers configured in {chat_dir}/config.json.[/yellow]")
                    console.print(f"[cyan]Detected SSH host aliases: {', '.join(ssh_hosts)}[/cyan]")
                    console.print(f"[dim]Run: optchat sync <peer> or add 'peers': [...] to config.json[/dim]")
                else:
                    console.print(f"[red]Error: No peer specified and no peers configured in {chat_dir}/config.json.[/red]")
                return

        client = EngineClient(socket_path=chat_dir / "engine.sock")
        use_daemon = client.is_daemon_alive()

        for peer in peers_to_sync:
            console.print(f"[cyan]Syncing memory idempotently with peer '{peer}' over SSH...[/cyan]")
            try:
                if use_daemon:
                    local_payload = client.sync_export()
                    remote_response = sync_payload_over_ssh(peer, local_payload, remote_dir=args.remote_dir)
                    res = client.sync_apply(remote_response)
                else:
                    storage = Storage(chat_dir)
                    storage.open()
                    try:
                        local_payload = export_sync_payload(storage)
                        remote_response = sync_payload_over_ssh(peer, local_payload, remote_dir=args.remote_dir)
                        res = apply_sync_payload(storage, remote_response)
                    finally:
                        storage.close()

                console.print(f"[green]Sync with '{peer}' complete! Total messages: {res.get('messages_count', 0)} (+{res.get('imported_messages', 0)} new, +{res.get('imported_nodes', 0)} tree nodes).[/green]")
            except Exception as e:
                console.print(f"[red]Sync with '{peer}' failed: {e}[/red]")

    elif args.subcommand == "sync-exchange":
        from optchat.engine_client import EngineClient
        from optchat.sync import export_sync_payload, apply_sync_payload
        chat_dir = Path(args.chat_dir)
        client = EngineClient(socket_path=chat_dir / "engine.sock")
        use_daemon = client.is_daemon_alive()

        input_data = sys.stdin.read()
        payload = {}
        if input_data.strip():
            try:
                payload = json.loads(input_data)
            except Exception as e:
                logger.warning("Failed to parse incoming sync payload: %s", e)

        if use_daemon:
            if payload:
                try:
                    client.sync_apply(payload)
                except Exception as e:
                    logger.warning("Daemon sync_apply failed: %s", e)
            response = client.sync_export()
        else:
            storage = Storage(chat_dir)
            storage.open()
            try:
                if payload:
                    try:
                        apply_sync_payload(storage, payload)
                    except Exception as e:
                        logger.warning("Storage apply_sync_payload failed: %s", e)
                response = export_sync_payload(storage)
            finally:
                storage.close()

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


    elif args.subcommand == "hook":
        from optchat.hooks import handle_hook_command
        handle_hook_command(args.event)

    elif args.subcommand == "install-hooks":
        from optchat.hooks import install_hooks
        ws = Path(args.workspace) if args.workspace else None
        target = install_hooks(workspace_dir=ws, global_config=(ws is None))
        console.print(f"[green]OptChat memory bridge hooks installed to {target}[/green]")

    elif args.subcommand == "uninstall-hooks":
        from optchat.hooks import uninstall_hooks
        ws = Path(args.workspace) if args.workspace else None
        target = uninstall_hooks(workspace_dir=ws, global_config=(ws is None))
        console.print(f"[yellow]OptChat memory bridge hooks removed from {target}[/yellow]")

    elif args.subcommand in ("device", "device-name", "whoami"):
        from optchat.storage import get_device_name_info
        from optchat.sync import get_configured_peers, get_ssh_config_hosts

        cfg_path = Path(os.path.expanduser("~/.optchat/config.json"))
        if getattr(args, "set_name", None):
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_data = {}
            if cfg_path.is_file():
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_data = json.load(f)
                except Exception:
                    pass
            cfg_data["device_name"] = args.set_name.strip()
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, indent=2)
            console.print(f"[green]Device name set to '{args.set_name.strip()}' in {cfg_path}[/green]")
            return

        name, source = get_device_name_info()
        if getattr(args, "verbose", False):
            console.print(f"[bold cyan]OptChat Device Name:[/bold cyan] {name}")
            console.print(f"[dim]Resolution Source:[/dim]   {source}")
            peers = get_configured_peers(cfg_path.parent)
            if peers:
                console.print(f"[dim]Configured Peers:[/dim]    {', '.join(peers)}")
            detected_ssh = get_ssh_config_hosts()
            if detected_ssh:
                console.print(f"[dim]SSH Host Aliases:[/dim]    {', '.join(detected_ssh)}")
        else:
            console.print(name)

    elif args.subcommand == "web":
        from optchat.web_server import run_web_app
        ws = Path(args.workspace) if args.workspace else None
        run_web_app(
            host=args.host,
            port=args.port,
            chat_dir=Path(args.chat_dir),
            provider_name=args.provider,
            model=args.model,
            compactor_model=args.compactor_model,
            workspace=ws,
            conversation_id=args.conversation,
        )

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
                workspace=getattr(args, "workspace", None),
                conversation_id=getattr(args, "conversation", None),
            )
        )


if __name__ == "__main__":
    main()
