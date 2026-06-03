"""
CLI entry point for ReadingTime — ``readingtime`` command.

Powered by ``click`` for argument parsing and ``rich`` for beautiful
terminal output.  Run ``readingtime --help`` for the full command list.

Usage::

    readingtime init       # First-time setup
    readingtime start      # Launch the agent daemon
    readingtime status     # View current shelf
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from readingtime.config import config
from readingtime.database import db

# ---------------------------------------------------------------------------
# Windows: force UTF-8 so emoji don't crash the terminal
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Rich console for pretty output
# ---------------------------------------------------------------------------
console = Console()

# PID file for daemon tracking
PID_FILE = Path("~/.readingtime/daemon.pid").expanduser()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    """Configure Python logging based on config.yaml settings."""
    log_file = config.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_file), encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def _resolve_shelf_path() -> Path:
    """Ensure shelf directory exists, return its path."""
    sp = config.shelf_path
    sp.mkdir(parents=True, exist_ok=True)
    return sp


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="0.1.0", prog_name="readingtime")
def cli() -> None:
    """📚 ReadingTime — AI agent that curates a shelf of 10 EPUB books
    and learns your reading taste from what you keep (and what you delete)."""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--force", is_flag=True, help="Overwrite existing config.yaml")
def init(force: bool) -> None:
    """First-time setup: create config, initialize database, fill shelf."""

    console.print("[bold cyan]📚 ReadingTime — Initializing...[/bold cyan]\n")

    # 1. Config
    config.initialize(force=force)
    console.print("✅ Configuration loaded")

    # 2. Database
    db.init_db()
    console.print("✅ Database initialized")

    # 3. Logging
    _setup_logging()
    console.print("✅ Logging configured")

    # 4. Shelf directory
    shelf_path = _resolve_shelf_path()
    console.print(f"✅ Shelf directory: [dim]{shelf_path}[/dim]")

    # 5. Seed the shelf with 10 books
    console.print("\n🔄 Seeding shelf with initial books (this may take a minute)...\n")

    from readingtime.shelf.manager import shelf_manager

    try:
        added = shelf_manager.initialize_shelf()
        if added > 0:
            console.print(f"\n[green]✅ Added {added} books to the shelf![/green]")
        else:
            console.print("\n[yellow]⚠ No books added — sources may be unreachable[/yellow]")
    except Exception as exc:
        console.print(f"\n[red]❌ Shelf seeding failed: {exc}[/red]")
        console.print("[yellow]You can retry later with: readingtime refill[/yellow]")

    # 6. Generate initial READING_TIME.md
    try:
        from readingtime.scheduler.tasks import regenerate_reading_time_md
        regenerate_reading_time_md()
        console.print("✅ READING_TIME.md generated")
    except Exception as exc:
        console.print(f"[yellow]⚠ READING_TIME.md generation skipped: {exc}[/yellow]")

    console.print("\n[bold green]🎉 ReadingTime is ready![/bold green]")
    console.print("Run [bold]readingtime start[/bold] to launch the agent.")


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

@cli.command()
def start() -> None:
    """Start the agent daemon (watcher + scheduler)."""

    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        console.print(f"[yellow]⚠ Daemon already running? PID file exists: {pid}[/yellow]")
        console.print("Run [bold]readingtime stop[/bold] first if you want to restart.")
        return

    _setup_logging()
    _resolve_shelf_path()
    db.init_db()

    console.print("[bold cyan]📚 Starting ReadingTime agent...[/bold cyan]")

    # Write PID
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    console.print(f"✅ PID {os.getpid()} written to {PID_FILE}")

    # Start scheduler in daemon thread
    from readingtime.scheduler.tasks import run_scheduler, stop_scheduler

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True, name="scheduler")
    scheduler_thread.start()
    console.print("✅ Scheduler started (daily expiry check + reading_time.md)")

    # Start file watcher in main thread
    from readingtime.monitor.watcher import ShelfWatcher

    watcher = ShelfWatcher()
    watcher.start()
    console.print(f"✅ Watcher started on [dim]{config.shelf_path}[/dim]")

    console.print("\n[green]🔍 Agent is running. Press Ctrl+C to stop.[/green]\n")

    # Graceful shutdown handler
    def _shutdown(signum, frame):
        console.print("\n[yellow]🛑 Shutting down...[/yellow]")
        stop_scheduler()
        watcher.stop()
        db.close()
        if PID_FILE.exists():
            PID_FILE.unlink()
        console.print("[green]✅ ReadingTime stopped.[/green]")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Keep main thread alive
    try:
        signal.pause()
    except AttributeError:
        # Windows doesn't have signal.pause()
        import time
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _shutdown(signal.SIGINT, None)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

@cli.command()
def stop() -> None:
    """Stop a running agent daemon."""

    if not PID_FILE.exists():
        console.print("[yellow]No PID file found — agent may not be running.[/yellow]")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        console.print(f"🛑 Stopping agent (PID {pid})...")
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink()
        console.print("[green]✅ Stopped.[/green]")
    except ProcessLookupError:
        console.print("[yellow]Process not found — cleaning up PID file.[/yellow]")
        PID_FILE.unlink()
    except Exception as exc:
        console.print(f"[red]❌ Failed to stop: {exc}[/red]")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
def status() -> None:
    """Show current shelf status."""

    db.init_db()
    books = db.get_current_books()
    shelf_path = _resolve_shelf_path()

    console.print(f"\n[bold]📚 ReadingTime Shelf[/bold] — [dim]{shelf_path}[/dim]")
    console.print(f"Books on shelf: [bold]{len(books)}[/bold] / {config.shelf_size}\n")

    if not books:
        console.print("[yellow]Shelf is empty. Run: readingtime refill[/yellow]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("书名", min_width=25)
    table.add_column("作者", min_width=15)
    table.add_column("语言", width=6)
    table.add_column("入架天数", width=8, justify="right")
    table.add_column("剩余天数", width=8, justify="right")
    table.add_column("来源", width=12)

    now = datetime.now(timezone.utc)
    for i, book in enumerate(books, 1):
        added_str = book.get("added_at", "")
        days_on = "?"
        days_left = "?"
        if added_str:
            try:
                added_at = datetime.fromisoformat(added_str)
                days_on = str((now - added_at).days)
                days_left = str(max(0, config.book_lifetime_days - (now - added_at).days))
            except (ValueError, TypeError):
                pass

        color = "red" if days_left != "?" and int(days_left) <= 3 else ""

        table.add_row(
            str(i),
            str(book.get("title", "?"))[:40],
            str(book.get("author", "?"))[:25],
            str(book.get("language", "en")).upper(),
            f"{days_on} 天",
            f"[{color}]{days_left} 天[/{color}]" if color else f"{days_left} 天",
            str(book.get("source", "?")),
        )

    console.print(table)

    # Show READING_TIME.md if it exists
    rt_md = shelf_path / "READING_TIME.md"
    if rt_md.exists():
        console.print(f"\n[dim]📄 {rt_md}[/dim]")


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

@cli.command()
def profile() -> None:
    """Show current user preference profile."""

    db.init_db()
    p = db.get_profile()

    if p is None:
        console.print("[yellow]No profile yet — start reading![/yellow]")
        console.print("Remove books you like, and the agent will learn.")
        return

    console.print("\n[bold cyan]🧠 Your Reading Profile[/bold cyan]\n")

    console.print("[bold]❤️  Liked Tags:[/bold]")
    liked_tags = p.get("liked_tags", [])
    if liked_tags:
        for tag in liked_tags[:10]:
            console.print(f"  • {tag}")
    else:
        console.print("  [dim](none yet)[/dim]")

    console.print("\n[bold]📝 Liked Authors:[/bold]")
    liked_authors = p.get("liked_authors", [])
    if liked_authors:
        for author in liked_authors[:10]:
            console.print(f"  • {author}")
    else:
        console.print("  [dim](none yet)[/dim]")

    console.print("\n[bold]😐 Neutral Tags:[/bold]")
    neutral_tags = p.get("neutral_tags", [])
    if neutral_tags:
        for tag in neutral_tags[:10]:
            console.print(f"  • {tag}")
    else:
        console.print("  [dim](none yet)[/dim]")

    console.print(f"\n[dim]Language preference: {p.get('lang_pref', 'en')}[/dim]")

    # Recent signals
    signals = db.get_recent_signals(limit=10)
    if signals:
        console.print("\n[bold cyan]📊 Recent Activity:[/bold cyan]")
        for s in signals[:10]:
            icon = "❤️" if s.get("signal") == "liked" else "😐"
            console.print(
                f"  {icon} {s.get('title', '?')} — {s.get('created_at', '?')[:19]}"
            )


# ---------------------------------------------------------------------------
# refill
# ---------------------------------------------------------------------------

@cli.command()
@click.option("-n", "--count", default=1, help="Number of books to add")
def refill(count: int) -> None:
    """Manually trigger a shelf refill."""

    _setup_logging()
    _resolve_shelf_path()
    db.init_db()

    from readingtime.shelf.manager import shelf_manager

    console.print(f"🔄 Refilling shelf (need {count} book(s))...\n")
    added = shelf_manager.refill(n=count)

    if added:
        console.print(f"\n[green]✅ Added {len(added)} book(s):[/green]")
        for f in added:
            console.print(f"  📖 {f}")
    else:
        console.print("\n[yellow]⚠ No books added — check your network or try again later.[/yellow]")


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

@cli.command()
def check() -> None:
    """Manually check for expired books (debugging)."""

    _setup_logging()
    _resolve_shelf_path()
    db.init_db()

    from readingtime.shelf.manager import shelf_manager

    console.print("🔍 Checking for expired books...\n")
    expired = shelf_manager.check_expirations()

    if expired > 0:
        console.print(f"[yellow]📦 {expired} book(s) expired and removed.[/yellow]")
    else:
        console.print("[green]✅ No books expired.[/green]")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query")
def add(query: str) -> None:
    """Manually search and add a book by title/author."""

    _setup_logging()
    _resolve_shelf_path()
    db.init_db()

    from readingtime.shelf.manager import shelf_manager

    console.print(f"🔍 Searching for: [bold]{query}[/bold] across all sources...\n")

    result = shelf_manager.add_single_book(query)

    if result:
        console.print(f"[green]✅ Added: {result}[/green]")

        current = shelf_manager.current_count()
        if current > config.shelf_size:
            console.print(
                f"[yellow]⚠ Shelf now has {current} books (limit: {config.shelf_size}). "
                f"Remove one to trigger learning.[/yellow]"
            )
    else:
        console.print("[yellow]No books found — try a different search term.[/yellow]")


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query", required=False, default=None)
def undo(query: Optional[str] = None) -> None:
    """Undo the most recent book removal (within 5 minutes).

    If a partial filename or title is given, undo that specific removal
    instead.  Restores the book to the shelf and cancels the 'liked'
    signal.
    """

    _setup_logging()
    db.init_db()

    from readingtime.shelf.manager import shelf_manager

    if query:
        # Fuzzy match against pending removals
        pending = db.get_all_pending_removals()
        matches = [
            p for p in pending
            if query.lower() in p.get("filename", "").lower()
            or query.lower() in p.get("title", "").lower()
        ]
        if not matches:
            console.print("[yellow]No pending removals matching that query.[/yellow]")
            return
        target = matches[0]
        result = shelf_manager.undo_removal(target["filename"])
    else:
        # Most recent pending removal
        pending = db.get_all_pending_removals()
        if not pending:
            console.print("[yellow]No pending removals to undo.[/yellow]")
            return
        # Most recently removed (first in list)
        result = shelf_manager.undo_removal(pending[0]["filename"])

    if result:
        console.print("[green]✅ Book restored![/green]")
    else:
        console.print("[yellow]⚠ Could not undo — the 5-minute window may have expired.[/yellow]")


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

@cli.command()
def history() -> None:
    """Show book history (all books that have been on the shelf)."""

    db.init_db()
    books = db.get_book_history(limit=50)

    console.print(f"\n[bold]📜 Book History[/bold] (last {len(books)} books)\n")

    if not books:
        console.print("[yellow]No history yet.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("书名", min_width=25)
    table.add_column("作者", min_width=15)
    table.add_column("入架日期", width=12)
    table.add_column("状态", width=12)
    table.add_column("移除原因", width=14)

    for book in books:
        added = book.get("added_at", "?")[:10] if book.get("added_at") else "?"
        removed = book.get("removed_at")
        removal_type = book.get("removal_type", "")

        if removed:
            status = "[red]已移除[/red]"
            reason = {
                "manual": "👤 用户喜欢",
                "auto_expired": "⏰ 自动过期",
                "system_init": "🔧 系统初始化",
            }.get(removal_type, removal_type)
        else:
            status = "[green]在架[/green]"
            reason = "—"

        table.add_row(
            str(book.get("title", "?"))[:40],
            str(book.get("author", "?"))[:25],
            added,
            status,
            reason,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Package entry point (called by ``readingtime`` console_script)."""
    cli()


if __name__ == "__main__":
    main()
