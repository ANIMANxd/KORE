import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

# Ensure .env loaded
from dotenv import load_dotenv
load_dotenv()

from engine.contradiction_detector import find_contradictions, save_contradiction_report
from engine.expiry_detector import scan_all_rules
from engine.graph import run_extraction
from engine.reextractor import find_affected_rules, reextract_rule
from ingestion.chunker import chunk_documents
from ingestion.loader import load
from output.schema import BusinessRule
from output.skills_writer import export_skills_json
from output.writer import load_rules, save_rule
from providers.config import ProviderConfig, get_config, load_config
from providers.embedder import embed_text
from providers.llm import test_connection
from providers.registry import get_provider, list_providers
from storage.vector_store import init_db, store_chunks, get_chunk_count, get_chunk_stats, reset_db as reset_vector_db, health_check

console = Console()

# ---------------------------------------------------------------------------
# Global verbose flag
# ---------------------------------------------------------------------------

_VERBOSE = False


def _debug(msg: str) -> None:
    if _VERBOSE:
        console.print(f"[dim][DEBUG] {msg}[/dim]")


# ---------------------------------------------------------------------------
# CLI group with verbose option
# ---------------------------------------------------------------------------

@click.group()
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, verbose: bool):
    """KORE - On-Premise Context Engine"""
    global _VERBOSE
    _VERBOSE = verbose
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


# ---------------------------------------------------------------------------
# Helpers reused across commands
# ---------------------------------------------------------------------------

def _pick_provider(prompt_text: str, provider_type: str | None = None) -> tuple[str, str]:
    providers = list_providers(provider_type)

    table = Table(title=prompt_text)
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Provider", style="magenta")
    table.add_column("Type", style="green")
    table.add_column("Notes", style="yellow")

    for i, name in enumerate(providers, 1):
        spec = get_provider(name)
        notes = []
        if spec.requires_api_key:
            notes.append("requires API key")
        if spec.requires_base_url:
            if spec.default_base_url:
                notes.append(f"base URL (default: {spec.default_base_url})")
            else:
                notes.append("requires base URL")
        if not spec.embedding_supported:
            notes.append("embedding not supported")
        table.add_row(str(i), name, spec.provider_type, "; ".join(notes) or "–")

    console.print(table)

    while True:
        choice = click.prompt("Select provider by number", type=int)
        if 1 <= choice <= len(providers):
            selected = providers[choice - 1]
            break
        console.print("[red]Invalid choice. Try again.[/red]")

    spec = get_provider(selected)
    console.print(f"\n[bold]{selected}[/bold] — {spec.model_name_hint}\n")
    model = click.prompt("Enter your model name exactly as your provider specifies it")
    return selected, model


def _ask_api_key(provider_name: str) -> str | None:
    spec = get_provider(provider_name)
    if spec.provider_type == "local":
        return None
    key = click.prompt("API key", hide_input=True)
    return key if key else None


def _ask_base_url(provider_name: str) -> str | None:
    spec = get_provider(provider_name)
    if spec.provider_type == "cloud" and not spec.requires_base_url:
        return None
    default = spec.default_base_url
    if default:
        url = click.prompt("Base URL", default=default, show_default=True)
    else:
        url = click.prompt("Base URL")
    return url if url else None


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

@cli.command()
def setup():
    """Interactive setup for provider.config.yaml"""
    config_path = Path("provider.config.yaml")

    if config_path.exists():
        if not click.confirm("Config found. Overwrite?"):
            console.print("[yellow]Setup cancelled.[/yellow]")
            return

    console.print(Panel.fit("KORE Provider Setup", style="bold blue"))

    # Step 1 — Deployment mode
    console.print("\n[bold]Step 1 — Deployment mode[/bold]")
    mode = click.prompt(
        "Choose mode",
        type=click.Choice(["cloud", "local", "hybrid"], case_sensitive=False),
        show_choices=True,
    )

    # Step 2 — LLM provider
    console.print("\n[bold]Step 2 — LLM provider[/bold]")
    llm_provider, llm_model = _pick_provider("Available LLM providers")
    llm_api_key = _ask_api_key(llm_provider)
    llm_base_url = _ask_base_url(llm_provider)
    llm_temperature = click.prompt("Temperature", type=float, default=0.1)

    # Step 4 — Embedding provider
    console.print("\n[bold]Step 4 — Embedding provider[/bold]")
    emb_provider, emb_model = _pick_provider("Available embedding providers")
    emb_api_key = _ask_api_key(emb_provider)
    emb_base_url = _ask_base_url(emb_provider)
    emb_dimensions = click.prompt(
        "Enter the output dimensions for this embedding model\n"
        "(check your provider's docs — common values: 768, 1024, 1536, 3072)",
        type=int,
    )

    # Step 5 — Vector store
    console.print("\n[bold]Step 5 — Vector store[/bold]")
    console.print("[bold]Where do you want KORE to store your data?[/bold]\n")
    console.print("[cyan][1][/cyan] LanceDB (recommended)")
    console.print("    Local file-based storage. Zero configuration.")
    console.print("    No server needed. Ships inside the KORE binary.\n")
    console.print("[cyan][2][/cyan] PostgreSQL + pgvector")
    console.print("    Connect to your existing PostgreSQL server.")
    console.print("    Requires pgvector extension to be installed.\n")

    vs_choice = click.prompt("Choose [1/2]", type=int)
    if vs_choice == 2:
        vector_store_backend = "pgvector"
        default_url = os.getenv("DATABASE_URL", "")
        pgvector_url = click.prompt(
            "PostgreSQL connection URL",
            default=default_url if default_url else "",
            show_default=bool(default_url),
        )
        lancedb_data_dir = "~/.kore/data"

        # Test connection
        console.print("\n[bold]Testing pgvector connection...[/bold]")
        try:
            import psycopg2
            conn = psycopg2.connect(pgvector_url)
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector';")
                has_vector = cur.fetchone()
            conn.close()
            if has_vector:
                console.print("  [green]✓[/green] Connected. pgvector extension found.")
            else:
                console.print("  [yellow]⚠[/yellow] pgvector extension [bold]not found[/bold].")
                console.print(
                    "    Run this in psql:\n"
                    "    [bold]CREATE EXTENSION IF NOT EXISTS vector;[/bold]\n"
                    "    Then re-run setup."
                )
                if not click.confirm("Save config anyway? (You must install pgvector before ingesting)"):
                    console.print("[yellow]Switching to LanceDB.[/yellow]")
                    vector_store_backend = "lancedb"
                    pgvector_url = None
        except Exception as exc:
            console.print(f"  [red]✗[/red] Connection failed: {exc}")
            if click.confirm("Switch to LanceDB instead?"):
                vector_store_backend = "lancedb"
                pgvector_url = None
            elif not click.confirm("Save config with pgvector anyway?"):
                console.print("[yellow]Setup cancelled.[/yellow]")
                return
    else:
        vector_store_backend = "lancedb"
        data_dir = click.prompt("Data directory", default="~/.kore/data", show_default=True)
        lancedb_data_dir = data_dir
        pgvector_url = None

    # Build temporary config for testing
    temp_config = ProviderConfig(
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_temperature=llm_temperature,
        embedding_provider=emb_provider,
        embedding_model=emb_model,
        embedding_api_key=emb_api_key,
        embedding_base_url=emb_base_url,
        embedding_dimensions=emb_dimensions,
        deployment_mode=mode,
        vector_store_backend=vector_store_backend,
        lancedb_data_dir=lancedb_data_dir,
        pgvector_url=pgvector_url,
    )

    # Step 6 — Test connections
    console.print("\n[bold]Step 6 — Test connections[/bold]")

    with console.status("[bold green]Testing LLM connection..."):
        llm_ok = test_connection(temp_config)

    if llm_ok:
        console.print("  LLM connection       [green]✓[/green]")
    else:
        console.print("  LLM connection       [red]✗[/red]")

    with console.status("[bold green]Testing embedding connection..."):
        try:
            emb_vec = embed_text("hello", config=temp_config)
            emb_ok = True
            emb_dims = len(emb_vec)
        except Exception as exc:
            emb_ok = False
            emb_dims = None
            console.print(f"  [dim]{exc}[/dim]")

    if emb_ok:
        console.print(f"  Embedding connection [green]✓[/green] ({emb_dims} dimensions)")
    else:
        console.print("  Embedding connection [red]✗[/red]")

    # Step 7 — Save
    console.print("\n[bold]Step 7 — Save configuration[/bold]")

    config_dict = {
        "llm": {
            "provider": llm_provider,
            "model": llm_model,
            "api_key": llm_api_key,
            "base_url": llm_base_url,
            "temperature": llm_temperature,
        },
        "embedding": {
            "provider": emb_provider,
            "model": emb_model,
            "api_key": emb_api_key,
            "base_url": emb_base_url,
            "dimensions": emb_dimensions,
        },
        "vector_store": {
            "backend": vector_store_backend,
            "lancedb": {
                "data_dir": lancedb_data_dir,
            },
            "pgvector": {
                "url": pgvector_url,
            },
        },
        "deployment_mode": mode,
    }

    header = (
        "# provider.config.yaml\n"
        "# Consult your provider's documentation for the latest available model names.\n"
        "# This codebase does not restrict or suggest specific models.\n\n"
    )
    config_path.write_text(header + yaml.safe_dump(config_dict, sort_keys=False), encoding="utf-8")

    gitignore = Path(".gitignore")
    entry = "provider.config.yaml\n"
    if gitignore.exists():
        current = gitignore.read_text(encoding="utf-8")
        if "provider.config.yaml" not in current:
            with open(gitignore, "a", encoding="utf-8") as f:
                if not current.endswith("\n"):
                    f.write("\n")
                f.write(entry)
    else:
        gitignore.write_text(entry, encoding="utf-8")

    console.print(
        "\n[bold green]Setup complete.[/bold green] "
        "Run [bold]python run.py status[/bold] to verify."
    )


# ---------------------------------------------------------------------------
# slack-setup
# ---------------------------------------------------------------------------

@cli.command("slack-setup")
def slack_setup():
    """Interactive setup for live Slack API ingestion"""
    console.print(Panel.fit("Slack Live Ingestion Setup", style="bold blue"))

    console.print("""
[bold]Prerequisites:[/bold]
1. Go to [link]https://api.slack.com/apps[/link] and click [bold]Create New App[/bold] → [bold]From scratch[/bold]
2. Name it "KORE Ingestion" and select your workspace
3. Go to [bold]OAuth & Permissions[/bold] → [bold]Bot Token Scopes[/bold]
4. Add these scopes:
   • channels:history   (read messages)
   • channels:read      (view channel info)
   • users:read         (map user IDs to names)
5. Click [bold]Install to Workspace[/bold] → [bold]Allow[/bold]
6. Copy the [bold]Bot User OAuth Token[/bold] (starts with xoxb-)
7. In each channel you want to ingest, type: /invite @kore-ingestion
""")

    if not click.confirm("Have you completed the steps above?"):
        console.print("[yellow]Setup cancelled. Complete the steps and run again.[/yellow]")
        return

    # Bot Token
    while True:
        token = click.prompt("Bot User OAuth Token", hide_input=True)
        if token.startswith("xoxb-"):
            break
        console.print("[red]Invalid token. Must start with 'xoxb-'. Try again.[/red]")

    # Channel IDs
    channel_ids = click.prompt(
        "Channel IDs (comma-separated, e.g. C01234567,C09876543)\n"
        "Tip: Right-click channel → Copy link → ID is the last segment"
    )

    # Earliest date
    if click.confirm("Set a date limit? (only ingest messages after this date)", default=False):
        earliest_date = click.prompt("Earliest date (YYYY-MM-DD)", default="")
    else:
        earliest_date = ""

    # Test connection
    console.print("\n[bold]Testing Slack connection...[/bold]")
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        auth = client.auth_test()
        console.print(f"  [green]✓[/green] Connected as [bold]{auth['user']}[/bold] in workspace [bold]{auth['team']}[/bold]")

        # Test each channel
        for ch_id in [c.strip() for c in channel_ids.split(",") if c.strip()]:
            try:
                info = client.conversations_info(channel=ch_id)
                ch_name = info["channel"]["name"]
                # Test history access
                hist = client.conversations_history(channel=ch_id, limit=1)
                msg_count = hist.get("messages", [])
                console.print(f"  [green]✓[/green] #{ch_name}: accessible ({len(msg_count)} messages in latest fetch)")
            except Exception as e:
                error = str(e)
                if "not_in_channel" in error:
                    console.print(f"  [red]✗[/red] {ch_id}: Bot not in channel. Run /invite @{auth['user']} in this channel.")
                else:
                    console.print(f"  [red]✗[/red] {ch_id}: {error}")
    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        if not click.confirm("Save config anyway?"):
            return

    # Save to .env
    env_path = Path(".env")
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    # Remove old Slack entries
    env_lines = [line for line in env_lines if not line.startswith(("SLACK_BOT_TOKEN=", "SLACK_CHANNEL_IDS=", "SLACK_EARLIEST_DATE="))]

    env_lines.append(f"SLACK_BOT_TOKEN={token}")
    env_lines.append(f"SLACK_CHANNEL_IDS={channel_ids}")
    if earliest_date:
        env_lines.append(f"SLACK_EARLIEST_DATE={earliest_date}")

    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    console.print("\n[green]✓ Saved Slack credentials to .env[/green]")

    if click.confirm("Run live ingestion now?"):
        ctx = click.get_current_context()
        ctx.invoke(ingest, source_type="slack", live=True)


# ---------------------------------------------------------------------------
# github-setup
# ---------------------------------------------------------------------------

@cli.command("github-setup")
def github_setup():
    """Interactive setup for live GitHub API ingestion"""
    console.print(Panel.fit("GitHub Live Ingestion Setup", style="bold blue"))

    console.print("""
[bold]Prerequisites:[/bold]
1. Go to [link]https://github.com/settings/tokens[/link] and click [bold]Generate new token (classic)[/bold]
2. Give it a name like "KORE Ingestion"
3. Select scopes:
   • [bold]repo[/bold] (for private repos) or [bold]public_repo[/bold] (for public repos)
   • [bold]read:user[/bold]
4. Click [bold]Generate token[/bold] and copy it (starts with ghp_)
""")

    if not click.confirm("Have you completed the steps above?"):
        console.print("[yellow]Setup cancelled. Complete the steps and run again.[/yellow]")
        return

    # Token
    while True:
        token = click.prompt("GitHub Personal Access Token", hide_input=True)
        if token.startswith(("ghp_", "github_pat_")):
            break
        console.print("[red]Invalid token. Must start with 'ghp_' or 'github_pat_'. Try again.[/red]")

    # Repo
    while True:
        repo_spec = click.prompt("Repo to ingest (format: owner/repo)")
        parts = repo_spec.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            break
        console.print("[red]Invalid format. Expected 'owner/repo'. Try again.[/red]")

    # Branch
    branch = click.prompt("Branch", default="main", show_default=True)

    # Extensions
    extensions = click.prompt(
        "File extensions to include (space-separated)",
        default=".py .md .yaml .yml .ts .js",
        show_default=True,
    )

    # Test connection
    console.print(f"\n[bold]Testing GitHub connection to {repo_spec}...[/bold]")
    try:
        from llama_index.readers.github.repository.github_client import GithubClient

        client = GithubClient(github_token=token)
        branch_data = client.get_branch(repo_spec, branch)
        if branch_data:
            console.print(f"  [green]✓[/green] Connected to [bold]{repo_spec}[/bold] ({branch} branch)")
        else:
            branches_raw = client.request("GET", f"/repos/{repo_spec}/branches")
            if branches_raw.status_code == 200:
                branch_names = [b["name"] for b in branches_raw.json()]
                console.print(f"  [green]✓[/green] Connected to [bold]{repo_spec}[/bold]")
                console.print(f"  [yellow]⚠[/yellow] Branch '{branch}' not found. Available: {', '.join(branch_names[:5])}{'...' if len(branch_names) > 5 else ''}")
                branch = click.prompt("Choose a branch", default=branch_names[0], show_default=True)
            else:
                console.print(f"  [red]✗[/red] API returned {branches_raw.status_code}")
                if not click.confirm("Save config anyway?"):
                    return
    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        if not click.confirm("Save config anyway?"):
            return

    # Save to .env
    env_path = Path(".env")
    env_lines: list[str] = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    env_lines = [
        line
        for line in env_lines
        if not line.startswith(
            ("GITHUB_TOKEN=", "GITHUB_REPO=", "GITHUB_BRANCH=", "GITHUB_EXTENSIONS=")
        )
    ]

    env_lines.append(f"GITHUB_TOKEN={token}")
    env_lines.append(f"GITHUB_REPO={repo_spec}")
    env_lines.append(f"GITHUB_BRANCH={branch}")
    env_lines.append(f"GITHUB_EXTENSIONS={extensions}")

    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    console.print("\n[green]✓ Saved GitHub credentials to .env[/green]")

    if click.confirm("Run live ingestion now?"):
        ctx = click.get_current_context()
        ctx.invoke(ingest, source_type="github", live=True)


# ---------------------------------------------------------------------------
# notion-setup
# ---------------------------------------------------------------------------

@cli.command("notion-setup")
def notion_setup():
    """Interactive setup for live Notion API ingestion"""
    console.print(Panel.fit("Notion Live Ingestion Setup", style="bold blue"))

    console.print("""
[bold]Prerequisites:[/bold]
1. Go to [link]https://www.notion.so/my-integrations[/link]
2. Click [bold]+ New integration[/bold]
3. Name it "KORE Ingestion" and select your workspace
4. Click [bold]Submit[/bold]
5. Copy the [bold]Internal Integration Secret[/bold] (starts with ntn_ or secret_)
6. For each page/database you want to ingest:
   • Open the page in Notion
   • Click [bold]...[/bold] (top-right) → [bold]Connections[/bold]
   • Add "KORE Ingestion"
""")

    if not click.confirm("Have you completed the steps above?"):
        console.print("[yellow]Setup cancelled. Complete the steps and run again.[/yellow]")
        return

    # Token
    while True:
        token = click.prompt("Notion Integration Token", hide_input=True)
        if token.startswith(("ntn_", "secret_")):
            break
        console.print("[red]Invalid token. Must start with 'ntn_' or 'secret_'. Try again.[/red]")

    # Test connection
    console.print("\n[bold]Testing Notion connection...[/bold]")
    try:
        from llama_index.readers.notion import NotionPageReader

        reader = NotionPageReader(integration_token=token)
        databases = reader.list_databases()
        pages = reader.list_pages()
        console.print(f"  [green]✓[/green] Connected successfully")
        console.print(f"  [dim]Databases found: {len(databases)}[/dim]")
        console.print(f"  [dim]Pages found: {len(pages)}[/dim]")
    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        console.print(
            "[dim]Make sure the integration has access to pages.\n"
            "In Notion, open each page → ... → Connections → add your integration.[/dim]"
        )
        if not click.confirm("Save config anyway?"):
            return

    # Save to .env
    env_path = Path(".env")
    env_lines: list[str] = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    env_lines = [
        line
        for line in env_lines
        if not line.startswith(("NOTION_TOKEN=", "NOTION_INTEGRATION_TOKEN="))
    ]

    env_lines.append(f"NOTION_TOKEN={token}")

    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    console.print("\n[green]✓ Saved Notion credentials to .env[/green]")

    if click.confirm("Run live ingestion now?"):
        ctx = click.get_current_context()
        ctx.invoke(ingest, source_type="notion", live=True)


# ---------------------------------------------------------------------------
# jira-setup
# ---------------------------------------------------------------------------

@cli.command("jira-setup")
def jira_setup():
    """Interactive setup for live Jira API ingestion"""
    console.print(Panel.fit("Jira Live Ingestion Setup", style="bold blue"))

    console.print("""
[bold]Prerequisites:[/bold]

[bold]1. Server URL[/bold]
   Open Jira in your browser. The URL is something like:
   [cyan]https://yourcompany.atlassian.net[/cyan]
   Copy that exact URL.

[bold]2. Username[/bold]
   Use the email address you log into Atlassian with.

[bold]3. API Token[/bold]
   a. Go to [link]https://id.atlassian.com/manage-profile/security/api-tokens[/link]
   b. Click [bold]Create API token[/bold]
   c. Label: "KORE Ingestion"
   d. Click [bold]Create[/bold] — a modal appears with your token
   e. Click [bold]Copy[/bold] immediately (you cannot view it again)
   f. The token is ~70 characters long, starts with letters/numbers

[bold]4. Project Key(s)[/bold]
   In Jira, open any project. The key is the short prefix on tickets
   (e.g. [bold]KAN-123[/bold] → project key is [bold]KAN[/bold]).
   You can find it in Project Settings → Details.
""")

    if not click.confirm("Have you completed the steps above?"):
        console.print("[yellow]Setup cancelled. Complete the steps and run again.[/yellow]")
        return

    # Server URL
    server_url = click.prompt(
        "Jira server URL (e.g. https://company.atlassian.net)"
    )

    # Username
    username = click.prompt("Jira username (your Atlassian email)")

    # API token
    api_token = click.prompt(
        "Jira API token (the ~70 char string from the API token page)",
        hide_input=True,
    )

    # Project keys
    project_keys = click.prompt(
        "Project keys (comma-separated, e.g. KAN,PROJ)"
    )

    # Test connection
    console.print("\n[bold]Testing Jira connection...[/bold]")
    try:
        from llama_index.readers.jira import JiraReader

        domain = server_url.replace("https://", "").replace("http://", "").strip("/")
        reader = JiraReader(
            email=username,
            api_token=api_token,
            server_url=domain,
        )
        keys = [k.strip() for k in project_keys.split(",") if k.strip()]
        if len(keys) == 1:
            jql = f"project = {keys[0]}"
        else:
            jql = f"project in ({', '.join(keys)})"

        docs = reader.load_data(query=jql, start_at=0, max_results=1)
        console.print(f"  [green]✓[/green] Connected successfully")
        console.print(f"  [dim]Projects: {', '.join(keys)} | Sample query returned {len(docs)} issue(s)[/dim]")
    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        if not click.confirm("Save config anyway?"):
            return

    # Save to .env
    env_path = Path(".env")
    env_lines: list[str] = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    env_lines = [
        line
        for line in env_lines
        if not line.startswith(
            ("JIRA_URL=", "JIRA_USERNAME=", "JIRA_API_TOKEN=", "JIRA_PROJECT_KEYS=")
        )
    ]

    env_lines.append(f"JIRA_URL={server_url}")
    env_lines.append(f"JIRA_USERNAME={username}")
    env_lines.append(f"JIRA_API_TOKEN={api_token}")
    env_lines.append(f"JIRA_PROJECT_KEYS={project_keys}")

    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    console.print("\n[green]✓ Saved Jira credentials to .env[/green]")

    if click.confirm("Run live ingestion now?"):
        ctx = click.get_current_context()
        ctx.invoke(ingest, source_type="jira", live=True)


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--source", "source_type", type=click.Choice(["slack", "github", "notion", "jira"], case_sensitive=False),
              required=True, help="Data source type")
@click.option("--path", required=False, help="Path to export directory (required for local mode)")
@click.option("--live", is_flag=True, help="Ingest via live API instead of local export")
def ingest(source_type: str, path: str | None, live: bool):
    """Ingest data: load → chunk → embed → store"""
    start = time.time()

    if source_type in ("github", "notion", "jira"):
        live = True

    if source_type not in ("slack", "github", "notion", "jira"):
        console.print(f"[red]Source '{source_type}' not yet implemented. Supported: slack, github, notion, jira.[/red]")
        return

    if live:
        console.print(Panel.fit(f"Ingesting live {source_type} data", style="bold blue"))
    else:
        if not path:
            console.print("[red]--path is required for local export mode. Use --live for API ingestion.[/red]")
            return
        console.print(Panel.fit(f"Ingesting {source_type} data from {path}", style="bold blue"))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        # Load
        task_load = progress.add_task("[cyan]Loading...", total=None)
        if live:
            docs = load(source_type, live=True)
        else:
            docs = load(source_type, path=path)
        progress.update(task_load, completed=1, total=1, description=f"[cyan]Loaded {len(docs)} documents")
        _debug(f"Loaded {len(docs)} raw documents")

        # Chunk
        task_chunk = progress.add_task("[green]Chunking...", total=None)
        chunks = chunk_documents(docs)
        progress.update(task_chunk, completed=1, total=1, description=f"[green]Created {len(chunks)} chunks")
        _debug(f"Created {len(chunks)} chunks")

        # Store
        task_store = progress.add_task("[magenta]Storing...", total=None)
        init_db()
        stored = store_chunks(chunks)
        progress.update(task_store, completed=1, total=1, description=f"[magenta]Stored {stored} chunks")
        _debug(f"Stored {stored} new chunks")

    elapsed = time.time() - start
    console.print(
        f"\n[bold green]Ingest complete.[/bold green] "
        f"{len(docs)} docs → {len(chunks)} chunks → {stored} stored "
        f"({elapsed:.1f}s)"
    )


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--query", required=True, help="Natural language query for the rule to extract")
@click.option("--save", is_flag=True, default=True, show_default=True,
              help="Save extracted rule to rules/extracted/")
def extract(query: str, save: bool):
    """Run the extraction pipeline for a query"""
    console.print(Panel.fit(f"Extracting rule for: {query}", style="bold blue"))

    start = time.time()
    final_state = run_extraction(query)
    elapsed = time.time() - start

    status = final_state.get("verification_status", "unknown")
    confidence = final_state.get("rule_confidence", 0.0)
    repairs = final_state.get("json_repair_attempts", 0)
    source_count = len(final_state.get("source_refs", []))
    rule_text = final_state.get("candidate_rule", "")

    # Status colour
    status_colour = {
        "verified": "green",
        "needs_review": "yellow",
        "rejected": "red",
        "parse_failed": "red",
    }.get(status, "white")

    console.print()
    console.print(Panel(
        f"[bold]{rule_text or '(no rule extracted)'}[/bold]\n\n"
        f"Confidence: [bold]{confidence:.2f}[/bold]  |  "
        f"Status: [bold {status_colour}]{status}[/bold {status_colour}]  |  "
        f"Sources: {source_count}  |  "
        f"Repair attempts: {repairs}  |  "
        f"Time: {elapsed:.1f}s",
        title="Extraction Result",
        border_style=status_colour,
    ))

    if status == "parse_failed":
        console.print(
            "\n[yellow]⚠ Warning: The LLM produced unparseable JSON even after repair.[/yellow]\n"
            "Suggestion: try a more capable model, or increase MAX_JSON_RETRIES in your .env"
        )

    if status == "rejected":
        reason = final_state.get("rejection_reason", "unknown")
        console.print(f"\n[dim]Rejection reason: {reason}[/dim]")

    if save and final_state.get("final_rule"):
        try:
            rule = BusinessRule(**final_state["final_rule"])
            path = save_rule(rule)
            console.print(f"\n[green]✓ Saved to {path}[/green]")

            # Auto-run contradiction detection against existing rules
            existing_rules = load_rules()
            contradictions = find_contradictions(rule, existing_rules)
            if contradictions:
                for report in contradictions:
                    save_contradiction_report(report)
                    console.print(
                        Panel(
                            f"[bold red]⚠ Contradiction detected with rule {report.rule_id_b[:8]}:[/bold red]\n"
                            f"{report.contradiction_description}\n"
                            f"[dim]Severity: {report.severity}[/dim]",
                            title="Rule Conflict",
                            border_style="red",
                        )
                    )
        except Exception as exc:
            console.print(f"\n[red]✗ Failed to save rule: {exc}[/red]")


# ---------------------------------------------------------------------------
# check-expiry
# ---------------------------------------------------------------------------

@cli.command("check-expiry")
@click.option("--days", default=90, show_default=True, help="Staleness threshold in days")
@click.option("--dir", "rules_dir", default="rules/extracted/", show_default=True, help="Directory containing rule YAML files")
def check_expiry(days: int, rules_dir: str):
    """Scan rules for staleness based on source reference dates"""
    console.print(Panel.fit(f"Checking rule expiry (>{days} days old)", style="bold blue"))

    reports = scan_all_rules(rules_dir=rules_dir, days_threshold=days)

    if not reports:
        console.print("[green]✓ All rules are fresh — no expired rules found.[/green]")
        return

    table = Table(title=f"Expired Rules ({len(reports)} found)")
    table.add_column("Rule Excerpt", style="white", max_width=45)
    table.add_column("Category", style="magenta")
    table.add_column("Last Referenced", style="cyan")
    table.add_column("Days Old", style="yellow", justify="right")

    for report in reports:
        # Colour-code severity by age
        days_old = report.days_since_referenced
        if days_old > 365:
            age_style = "bold red"
        elif days_old > 180:
            age_style = "red"
        else:
            age_style = "yellow"

        table.add_row(
            report.rule_text,
            report.rule_category,
            report.last_source_date.strftime("%Y-%m-%d"),
            f"[{age_style}]{days_old}[/{age_style}]",
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(reports)} rule(s) have not been referenced in the last {days} days. "
        "Run [bold]python run.py extract --query ... --save[/bold] to re-extract from current data.[/dim]"
    )


# ---------------------------------------------------------------------------
# reextract
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--rule-id", required=True, help="Full UUID of the rule to re-extract")
@click.option("--query", required=False, help="Custom query (defaults to existing rule text)")
@click.option("--save", is_flag=True, default=False, show_default=True, help="Auto-save without prompting")
def reextract(rule_id: str, query: str | None, save: bool):
    """Re-run extraction for an existing rule and optionally update it"""
    rules = load_rules()
    rule = next((r for r in rules if r.rule_id == rule_id), None)

    if rule is None:
        console.print(f"[red]✗ Rule not found: {rule_id}[/red]")
        return

    console.print(Panel.fit(f"Re-extracting rule: {rule.rule_text[:60]}...", style="bold blue"))

    start = time.time()
    final_state = reextract_rule(rule, query=query)
    elapsed = time.time() - start

    new_text = final_state.get("candidate_rule", "")
    new_confidence = final_state.get("rule_confidence", 0.0)
    new_status = final_state.get("verification_status", "unknown")
    new_sources = len(final_state.get("source_refs", []))

    # Side-by-side comparison
    old_panel = Panel(
        f"[bold]{rule.rule_text}[/bold]\n\n"
        f"Confidence: [bold]{rule.confidence:.2f}[/bold]  |  "
        f"Status: [bold]{rule.verification_status}[/bold]  |  "
        f"Sources: {len(rule.source_refs)}  |  "
        f"Version: {rule.version}",
        title=f"Old Rule  (v{rule.version})",
        border_style="dim",
    )

    status_colour = {
        "verified": "green",
        "needs_review": "yellow",
        "rejected": "red",
        "parse_failed": "red",
    }.get(new_status, "white")

    new_panel = Panel(
        f"[bold]{new_text or '(no rule extracted)'}[/bold]\n\n"
        f"Confidence: [bold]{new_confidence:.2f}[/bold]  |  "
        f"Status: [bold {status_colour}]{new_status}[/bold {status_colour}]  |  "
        f"Sources: {new_sources}  |  "
        f"Time: {elapsed:.1f}s",
        title="New Rule",
        border_style=status_colour,
    )

    console.print()
    console.print(old_panel)
    console.print(new_panel)

    if new_status in ("rejected", "parse_failed"):
        console.print("\n[yellow]⚠ New extraction failed or was rejected. Keeping existing rule.[/yellow]")
        return

    if save:
        should_replace = True
    else:
        should_replace = click.confirm("\nReplace existing rule with new version?")

    if should_replace:
        updates = {
            "rule_text": new_text,
            "confidence": new_confidence,
            "verification_status": new_status,
            "source_refs": [
                {
                    "chunk_id": s["chunk_id"],
                    "source_type": s["source_type"],
                    "channel": s.get("channel", ""),
                    "timestamp": s.get("timestamp", ""),
                    "excerpt": s.get("excerpt", ""),
                }
                for s in final_state.get("source_refs", [])
            ],
            "ambiguity_notes": f"Re-extracted from query: {query or rule.rule_text}",
        }
        updated = update_rule(rule_id, updates)
        console.print(f"\n[green]✓ Updated to version {updated.version}[/green]")
    else:
        console.print("\n[dim]Kept existing rule. No changes made.[/dim]")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@cli.command()
def review():
    """Interactive human review of rules flagged for review"""
    rules = load_rules()
    needs_review = [r for r in rules if r.verification_status == "needs_review"]

    if not needs_review:
        console.print("[green]No rules awaiting review.[/green]")
        return

    console.print(Panel.fit(f"{len(needs_review)} rule(s) need review", style="bold yellow"))

    table = Table(title="Rules Awaiting Review")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Rule", style="white", max_width=50)
    table.add_column("Category", style="magenta")
    table.add_column("Confidence", style="green")
    table.add_column("Version", style="yellow")

    for i, rule in enumerate(needs_review, 1):
        table.add_row(
            str(i),
            rule.rule_text[:60] + "..." if len(rule.rule_text) > 60 else rule.rule_text,
            rule.rule_category,
            f"{rule.confidence:.2f}",
            str(rule.version),
        )
    console.print(table)

    for rule in needs_review:
        console.print(f"\n[bold]Rule:[/bold] {rule.rule_text}")
        console.print(f"[dim]Category: {rule.rule_category} | Confidence: {rule.confidence:.2f} | Version: {rule.version}[/dim]")
        action = click.prompt(
            "Action: (a)pprove / (r)eject / (e)dit / (s)kip",
            type=click.Choice(["a", "r", "e", "s"], case_sensitive=False),
        )

        if action == "a":
            from output.writer import update_rule
            updated = update_rule(
                rule.rule_id,
                {"verification_status": "verified", "approved_by": "human_review"},
            )
            console.print(f"[green]✓ Approved (version {updated.version})[/green]")

        elif action == "r":
            reason = click.prompt("Rejection reason")
            from output.writer import update_rule
            updated = update_rule(
                rule.rule_id,
                {"verification_status": "rejected", "ambiguity_notes": reason},
            )
            console.print(f"[red]✗ Rejected (version {updated.version})[/red]")

        elif action == "e":
            new_text = click.edit(rule.rule_text)
            if new_text and new_text.strip() != rule.rule_text.strip():
                from output.writer import update_rule
                updated = update_rule(
                    rule.rule_id,
                    {"rule_text": new_text.strip()},
                )
                console.print(f"[yellow]✎ Edited (version {updated.version})[/yellow]")
            else:
                console.print("[dim]No changes made.[/dim]")

        elif action == "s":
            console.print("[dim]Skipped.[/dim]")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
def status():
    """Show system status dashboard"""
    try:
        cfg = get_config()
    except FileNotFoundError:
        console.print("[red]No provider.config.yaml found. Run `python run.py setup` first.[/red]")
        return

    console.print(Panel.fit("KORE Status Dashboard", style="bold blue"))

    # Provider info
    console.print("\n[bold]Providers[/bold]")
    console.print(f"  Deployment mode: [cyan]{cfg.deployment_mode}[/cyan]")
    console.print(f"  LLM:             [cyan]{cfg.llm_provider}[/cyan] / [magenta]{cfg.llm_model}[/magenta]")
    console.print(f"  Embedding:       [cyan]{cfg.embedding_provider}[/cyan] / [magenta]{cfg.embedding_model}[/magenta]")
    console.print(f"  Dimensions:      [cyan]{cfg.embedding_dimensions}[/cyan]")

    # Vector store stats
    console.print("\n[bold]Vector Store[/bold]")
    try:
        total_chunks = get_chunk_count()
        console.print(f"  Total chunks:    [green]{total_chunks}[/green]")
        console.print(f"  Backend:         [cyan]{cfg.vector_store_backend}[/cyan]")

        stats = get_chunk_stats()
        for src, cnt in sorted(stats.items()):
            console.print(f"    {src}: {cnt}")
    except Exception as exc:
        console.print(f"  [red]Store unavailable: {exc}[/red]")

    # Rules stats
    console.print("\n[bold]Extracted Rules[/bold]")
    rules = load_rules()
    if rules:
        status_counts = Counter(r.verification_status for r in rules)
        cat_counts = Counter(r.rule_category for r in rules)

        console.print(f"  Total rules:     [green]{len(rules)}[/green]")
        console.print("  By status:")
        for st, cnt in sorted(status_counts.items()):
            colour = {"verified": "green", "needs_review": "yellow", "rejected": "red", "parse_failed": "red"}.get(st, "white")
            console.print(f"    [{colour}]{st}[/{colour}]: {cnt}")

        console.print("  By category:")
        for cat, cnt in sorted(cat_counts.items()):
            console.print(f"    {cat}: {cnt}")

        # Repair stats
        total_repairs = sum(
            r.extraction_meta.json_repair_attempts for r in rules if r.extraction_meta
        )
        avg_repairs = total_repairs / len(rules) if rules else 0
        console.print(f"  JSON repairs:    total={total_repairs}, avg={avg_repairs:.1f}")

        # Last extraction
        try:
            latest = max(r.created_at for r in rules if r.created_at)
            console.print(f"  Last extraction: {latest}")
        except Exception:
            pass
    else:
        console.print("  [dim]No rules extracted yet.[/dim]")


# ---------------------------------------------------------------------------
# export-skills
# ---------------------------------------------------------------------------

@cli.command("export-skills")
@click.option("--output", default="rules/skills.json", show_default=True, help="Output JSON file path")
@click.option("--dir", "rules_dir", default="rules/extracted/", show_default=True, help="Directory containing rule YAML files")
def export_skills(output: str, rules_dir: str):
    """Export verified, approved rules to skills.json"""
    console.print(Panel.fit("Exporting Skills", style="bold blue"))

    try:
        path = export_skills_json(rules_dir=rules_dir, output_path=output)
        # Load the file to report the count
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        console.print(f"[green]✓ Exported {data['total_skills']} skill(s) to {path}[/green]")
    except Exception as exc:
        console.print(f"[red]✗ Export failed: {exc}[/red]")


# ---------------------------------------------------------------------------
# reset-db
# ---------------------------------------------------------------------------

@cli.command()
def reset_db():
    """Drop and recreate the vector store"""
    console.print(Panel.fit("Reset Database", style="bold red"))
    console.print("[red]This deletes all stored chunks.[/red]")

    if not click.confirm("Are you sure?"):
        console.print("[yellow]Cancelled.[/yellow]")
        return

    try:
        reset_vector_db()
        console.print("[green]✓ Vector store reset complete.[/green]")
    except Exception as exc:
        console.print(f"[red]✗ Failed: {exc}[/red]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
