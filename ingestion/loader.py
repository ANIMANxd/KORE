"""
Data loader supporting both local exports and live API ingestion.

Local exports are parsed directly from JSON.
Live ingestion uses the Slack SDK (installed with llama-index-readers-slack).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from llama_index.core.schema import Document

load_dotenv()


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_valid_message(msg: dict) -> bool:
    if msg.get("type") != "message":
        return False
    if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
        return False
    text = msg.get("text", "").strip()
    if not text:
        return False
    if msg.get("bot_id") or msg.get("user", "").startswith("B"):
        return False
    return True


def _parse_ts(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts))


def _load_slack_export(
    export_path: str,
    channels: list[str] | None = None,
) -> list[Document]:
    """Load a local Slack export directory into LlamaIndex Documents."""
    root = Path(export_path)
    if not root.exists():
        raise FileNotFoundError(f"Slack export directory not found: {root}")

    users_data = _load_json(root / "users.json") if (root / "users.json").exists() else []
    user_map = {u["id"]: u.get("name", u.get("real_name", u["id"])) for u in users_data}

    channels_data = (
        _load_json(root / "channels.json") if (root / "channels.json").exists() else []
    )
    channel_map = {c["id"]: c["name"] for c in channels_data}

    docs: list[Document] = []

    for ch_dir in root.iterdir():
        if not ch_dir.is_dir():
            continue
        ch_name = ch_dir.name
        if channels and ch_name not in channels:
            continue

        channel_id = None
        for c in channels_data:
            if c["name"] == ch_name:
                channel_id = c["id"]
                break

        daily_files = sorted(ch_dir.glob("*.json"))
        for day_file in daily_files:
            messages = _load_json(day_file)
            if not isinstance(messages, list):
                continue

            for msg in messages:
                if not _is_valid_message(msg):
                    continue

                user_id = msg.get("user", "")
                ts = msg.get("ts", "")
                thread_ts = msg.get("thread_ts")
                text = msg["text"].strip()
                user_name = user_map.get(user_id, user_id)
                dt = _parse_ts(ts)

                metadata = {
                    "source_type": "slack",
                    "channel": f"#{ch_name}",
                    "channel_id": channel_id,
                    "timestamp": dt.isoformat() + "Z",
                    "user_id": user_id,
                    "user_name": user_name,
                    "message_id": ts,
                    "thread_ts": thread_ts,
                    "date_file": day_file.stem,
                }

                doc = Document(
                    text=text,
                    metadata=metadata,
                    id_=f"slack_{ch_name}_{ts.replace('.', '_')}",
                )
                docs.append(doc)

    return docs


def _load_slack_live() -> list[Document]:
    """Load messages from live Slack API using the Slack SDK.

    Requires environment variables:
      SLACK_BOT_TOKEN - Bot User OAuth Token (xoxb-...)
      SLACK_CHANNEL_IDS - Comma-separated list of channel IDs (e.g. C01234567,C09876543)
      SLACK_EARLIEST_DATE - ISO date string, optional (default: 90 days ago)

    The bot must be invited to each channel and have these scopes:
      channels:history, channels:read, users:read
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN not set. Add it to your .env file.\n"
            "Create a Slack app at https://api.slack.com/apps, install it to your workspace,\n"
            "and copy the Bot User OAuth Token (starts with xoxb-).\n"
            "Required bot token scopes: channels:history, channels:read, users:read"
        )

    channel_ids_raw = os.getenv("SLACK_CHANNEL_IDS")
    if not channel_ids_raw:
        raise RuntimeError(
            "SLACK_CHANNEL_IDS not set. Add it to your .env file as a comma-separated list,\n"
            "e.g. SLACK_CHANNEL_IDS=C01234567,C09876543\n"
            "Find channel IDs in Slack: right-click channel name → Copy link → ID is the last segment."
        )
    channel_ids = [c.strip() for c in channel_ids_raw.split(",") if c.strip()]

    earliest_date_str = os.getenv("SLACK_EARLIEST_DATE")
    if earliest_date_str:
        earliest_dt = datetime.fromisoformat(earliest_date_str)
        oldest_ts = str(earliest_dt.timestamp())
    else:
        from datetime import timedelta
        oldest_dt = datetime.now(timezone.utc) - timedelta(days=90)
        oldest_ts = str(oldest_dt.timestamp())

    client = WebClient(token=token)

    # Fetch users for name mapping
    try:
        users_result = client.users_list()
        user_map = {
            u["id"]: u.get("name", u.get("real_name", u["id"]))
            for u in users_result.get("members", [])
        }
    except SlackApiError:
        user_map = {}

    docs: list[Document] = []

    for channel_id in channel_ids:
        try:
            # Get channel info for name
            channel_info = client.conversations_info(channel=channel_id)
            channel_name = channel_info.get("channel", {}).get("name", channel_id)
        except SlackApiError:
            channel_name = channel_id

        # Fetch messages
        next_cursor = None
        while True:
            kwargs = {
                "channel": channel_id,
                "cursor": next_cursor,
                "oldest": oldest_ts,
                "limit": 200,
            }
            result = client.conversations_history(**kwargs)
            messages = result.get("messages", [])

            for msg in messages:
                text = msg.get("text", "").strip()
                if not text:
                    continue
                if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                    continue
                if msg.get("bot_id") or msg.get("user", "").startswith("B"):
                    continue

                user_id = msg.get("user", "")
                ts = msg.get("ts", "")
                thread_ts = msg.get("thread_ts")
                user_name = user_map.get(user_id, user_id)
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)

                metadata = {
                    "source_type": "slack",
                    "channel": f"#{channel_name}",
                    "channel_id": channel_id,
                    "timestamp": dt.isoformat(),
                    "user_id": user_id,
                    "user_name": user_name,
                    "message_id": ts,
                    "thread_ts": thread_ts,
                }

                doc = Document(
                    text=text,
                    metadata=metadata,
                    id_=f"slack_{channel_name}_{ts.replace('.', '_')}",
                )
                docs.append(doc)

            if not result.get("has_more"):
                break
            next_cursor = result.get("response_metadata", {}).get("next_cursor")

    return docs


def _load_github() -> list[Document]:
    from llama_index.readers.github import (
        GithubRepositoryReader,
        GitHubRepositoryIssuesReader,
    )
    from llama_index.readers.github.repository.github_client import (
        GithubClient,
    )
    from llama_index.readers.github.issues.github_client import (
        GitHubIssuesClient,
    )

    import click as _click
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
    )

    console = Console()

    token = os.getenv("GITHUB_TOKEN")
    repo_spec = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")
    extensions_raw = os.getenv("GITHUB_EXTENSIONS")

    if not token or not repo_spec:
        console.print(Panel("[bold]GitHub Setup[/bold]", style="bold blue"))
        console.print("[dim]Required: Personal Access Token + repo to ingest.[/dim]\n")

        if not token:
            token = _click.prompt(
                "Enter your GitHub Personal Access Token",
                hide_input=True,
            )
        if not repo_spec:
            repo_spec = _click.prompt(
                "Enter repo to ingest (format: owner/repo)"
            )
        if not os.getenv("GITHUB_BRANCH"):
            branch = _click.prompt(
                "Enter branch", default="main", show_default=True
            )
        if not extensions_raw:
            extensions_raw = _click.prompt(
                "File extensions to include",
                default=".py .md .yaml .yml .ts .js",
                show_default=True,
            )

        _write_github_env(token, repo_spec, branch, extensions_raw)

    parts = repo_spec.split("/")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid repo format '{repo_spec}'. Expected 'owner/repo'."
        )
    owner, repo_name = parts

    extensions = [
        ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}"
        for ext in extensions_raw.split()
    ]

    repo_client = GithubClient(github_token=token)
    issues_client = GitHubIssuesClient(github_token=token)

    repo_reader = GithubRepositoryReader(
        github_client=repo_client,
        owner=owner,
        repo=repo_name,
        filter_directories=(
            [
                "node_modules",
                ".git",
                "__pycache__",
                "dist",
                "build",
                "venv",
                ".venv",
                "target",
                ".next",
                "coverage",
                ".tox",
                ".eggs",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "vendor",
                "bower_components",
                ".yarn",
                ".cache",
                ".terraform",
            ],
            GithubRepositoryReader.FilterType.EXCLUDE,
        ),
        filter_file_extensions=(
            extensions,
            GithubRepositoryReader.FilterType.INCLUDE,
        ),
        timeout=30,
        fail_on_error=False,
    )

    issues_reader = GitHubRepositoryIssuesReader(
        github_client=issues_client,
        owner=owner,
        repo=repo_name,
    )

    all_docs: list[Document] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        console=console,
    ) as progress:
        task_repo = progress.add_task(
            f"Fetching GitHub repo files...",
            total=None,
        )
        try:
            repo_docs = repo_reader.load_data(branch=branch)
        except Exception as exc:
            console.print(f"[red]Error loading repo files: {exc}[/red]")
            repo_docs = []

        for doc in repo_docs:
            metadata = dict(doc.metadata)
            metadata["source_type"] = "github"
            metadata["repo"] = repo_spec
            metadata["branch"] = branch
            doc.metadata = metadata

        all_docs.extend(repo_docs)
        progress.update(
            task_repo,
            completed=1,
            total=1,
            description=(
                f"[cyan]Loaded {len(repo_docs)} files "
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%"
            ),
        )

        task_issues = progress.add_task(
            "Fetching GitHub issues...",
            total=None,
        )
        try:
            issue_docs = []
            for state in (
                GitHubRepositoryIssuesReader.IssueState.OPEN,
                GitHubRepositoryIssuesReader.IssueState.CLOSED,
            ):
                state_issues = issues_reader.load_data(state=state)
                issue_docs.extend(state_issues)
        except Exception:
            issue_docs = []

        for doc in issue_docs:
            metadata = dict(doc.metadata)
            metadata["source_type"] = "github"
            metadata["repo"] = repo_spec
            metadata["branch"] = branch
            metadata["issue_number"] = str(doc.doc_id) if doc.doc_id else ""
            doc.metadata = metadata

        all_docs.extend(issue_docs)
        progress.update(
            task_issues,
            completed=1,
            total=1,
            description=(
                f"[cyan]Loaded {len(issue_docs)} issues "
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%"
            ),
        )

    console.print(
        f"[bold]GitHub:[/bold] loaded {len(all_docs)} documents "
        f"from {repo_spec} ({branch} branch)"
    )

    return all_docs


def _write_github_env(
    token: str, repo: str, branch: str, extensions: str
) -> None:
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
    env_lines.append(f"GITHUB_REPO={repo}")
    env_lines.append(f"GITHUB_BRANCH={branch}")
    env_lines.append(f"GITHUB_EXTENSIONS={extensions}")

    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print("[KORE] Saved GitHub credentials to .env")


def _load_notion() -> list[Document]:
    from llama_index.readers.notion import NotionPageReader

    import click as _click
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
    )

    console = Console()

    token = os.getenv("NOTION_TOKEN") or os.getenv("NOTION_INTEGRATION_TOKEN")

    if not token:
        console.print(Panel("[bold]Notion Setup[/bold]", style="bold blue"))
        console.print(
            "[dim]A Notion integration token is required to read pages.[/dim]\n"
        )
        console.print("""
[bold]How to create a Notion integration:[/bold]
1. Go to [link]https://www.notion.so/my-integrations[/link]
2. Click [bold]+ New integration[/bold]
3. Name it "KORE Ingestion" and select your workspace
4. Click [bold]Submit[/bold] and copy the [bold]Internal Integration Secret[/bold]
5. Go to each page/database you want to ingest → [bold]...[/bold] → [bold]Connections[/bold]
6. Add "KORE Ingestion" to grant access
""")
        token = _click.prompt(
            "Enter your Notion Integration Token",
            hide_input=True,
        )
        _write_notion_env(token)

    reader = NotionPageReader(integration_token=token)

    all_docs: list[Document] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        console=console,
    ) as progress:
        task_pages = progress.add_task(
            "Fetching Notion pages...",
            total=None,
        )
        try:
            docs = reader.load_data(load_all_if_empty=True)
        except Exception as exc:
            console.print(
                f"[red]Error loading Notion pages: {exc}[/red]\n"
                f"[dim]Make sure the integration has access to the pages/databases "
                f"you want to ingest. In Notion, open each page → ... → Connections → "
                f"add your integration.[/dim]"
            )
            docs = []

        for doc in docs:
            metadata = dict(doc.metadata) if doc.metadata else {}
            existing_page_id = (
                metadata.get("page_id")
                or str(doc.doc_id)
            )
            metadata["source_type"] = "notion"
            metadata["page_id"] = existing_page_id
            metadata["page_title"] = ""
            doc.metadata = metadata

        all_docs.extend(docs)
        progress.update(
            task_pages,
            completed=1,
            total=1,
            description=(
                f"[cyan]Loaded {len(docs)} pages "
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%"
            ),
        )

    console.print(
        f"[bold]Notion:[/bold] loaded {len(all_docs)} pages"
    )

    return all_docs


def _write_notion_env(token: str) -> None:
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
    print("[KORE] Saved Notion credentials to .env")


def _load_jira() -> list[Document]:
    from llama_index.readers.jira import JiraReader

    import click as _click
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
    )

    console = Console()

    server_url = os.getenv("JIRA_URL", "")
    username = os.getenv("JIRA_USERNAME", "")
    api_token = os.getenv("JIRA_API_TOKEN", "")
    project_keys_raw = os.getenv("JIRA_PROJECT_KEYS", "")

    if not all((server_url, username, api_token, project_keys_raw)):
        console.print(Panel("[bold]Jira Setup[/bold]", style="bold blue"))
        console.print(
            "[dim]Jira Cloud credentials are required to read issues.[/dim]\n"
        )
        console.print("""
[bold]How to get your Jira Cloud credentials:[/bold]

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
        if not server_url:
            server_url = _click.prompt(
                "Jira server URL (e.g. https://company.atlassian.net)"
            )
        if not username:
            username = _click.prompt("Jira username (your Atlassian email)")
        if not api_token:
            api_token = _click.prompt(
                "Jira API token (the ~70 char string from the API token page)",
                hide_input=True,
            )
        if not project_keys_raw:
            project_keys_raw = _click.prompt(
                "Project keys (comma-separated, e.g. KAN,PROJ)"
            )

        _write_jira_env(server_url, username, api_token, project_keys_raw)

    # Strip protocol if present — JiraReader prepends https:// internally
    domain = server_url.replace("https://", "").replace("http://", "").strip("/")

    project_keys = [k.strip() for k in project_keys_raw.split(",") if k.strip()]
    if len(project_keys) == 1:
        jql = f"project = {project_keys[0]}"
    else:
        jql = f"project in ({', '.join(project_keys)})"

    reader = JiraReader(
        email=username,
        api_token=api_token,
        server_url=domain,
    )

    all_docs: list[Document] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        console=console,
    ) as progress:
        task_jira = progress.add_task(
            f"Fetching Jira issues ({', '.join(project_keys)})...",
            total=None,
        )

        try:
            # NOTE: the jira library auto-paginates internally.
            # max_results=50 here is the *page size* for each API call,
            # not a hard limit on total issues returned.
            docs = reader.load_data(query=jql, start_at=0, max_results=50)

            for doc in docs:
                metadata = dict(doc.metadata) if doc.metadata else {}
                metadata["source_type"] = "jira"
                metadata["ticket_id"] = str(doc.doc_id) if doc.doc_id else ""
                metadata["status"] = metadata.get("status", "")
                metadata["assignee"] = metadata.get("assignee", "")
                doc.metadata = metadata

            all_docs.extend(docs)

        except Exception as exc:
            console.print(f"[red]Error loading Jira issues: {exc}[/red]")

        progress.update(
            task_jira,
            completed=1,
            total=1,
            description=(
                f"[cyan]Loaded {len(all_docs)} issues "
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%"
            ),
        )

    console.print(
        f"[bold]Jira:[/bold] loaded {len(all_docs)} issues "
        f"from projects: {', '.join(project_keys)}"
    )

    return all_docs


def _write_jira_env(
    server_url: str, username: str, api_token: str, project_keys: str
) -> None:
    env_path = Path(".env")
    env_lines: list[str] = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    env_lines = [
        line
        for line in env_lines
        if not line.startswith(
            (
                "JIRA_URL=",
                "JIRA_USERNAME=",
                "JIRA_API_TOKEN=",
                "JIRA_PROJECT_KEYS=",
            )
        )
    ]

    env_lines.append(f"JIRA_URL={server_url}")
    env_lines.append(f"JIRA_USERNAME={username}")
    env_lines.append(f"JIRA_API_TOKEN={api_token}")
    env_lines.append(f"JIRA_PROJECT_KEYS={project_keys}")

    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print("[KORE] Saved Jira credentials to .env")


def load(
    source: Literal["slack", "github", "notion", "jira"],
    path: str | None = None,
    live: bool = False,
    **kwargs: Any,
) -> list[Document]:
    """Load data from a source.

    Parameters
    ----------
    source : {"slack", "github", "notion", "jira"}
        The data source type.
    path : str | None
        Path to local export directory. Required for local mode.
    live : bool
        If True, fetch data via live API instead of local export.
    **kwargs
        Additional arguments passed to the loader.

    Returns
    -------
    list[Document]
        LlamaIndex Document objects with metadata["source_type"] set.

    Raises
    ------
    NotImplementedError
        For sources not yet supported (github, notion, jira).
    FileNotFoundError
        If path is provided but does not exist.
    RuntimeError
        If live mode is selected but required env vars are missing.
    """
    if source == "slack":
        if live:
            return _load_slack_live()
        if path is None:
            raise ValueError("path is required for local Slack export loading")
        return _load_slack_export(path, **kwargs)

    if source == "github":
        if live:
            return _load_github()
        raise NotImplementedError(
            "Local GitHub export loading is not supported. Use --live to fetch from the API."
        )

    if source == "notion":
        if live:
            return _load_notion()
        raise NotImplementedError(
            "Local Notion export loading is not supported. Use --live to fetch from the API."
        )

    if source == "jira":
        if live:
            return _load_jira()
        raise NotImplementedError(
            "Local Jira export loading is not supported. Use --live to fetch from the API."
        )

    raise NotImplementedError(f"Source '{source}' is coming soon. Only 'slack', 'github', 'notion', and 'jira' are supported.")


if __name__ == "__main__":
    # Test local export
    docs = load("slack", path="data/sample")
    print(f"Loaded {len(docs)} documents from local export\n")

    for i, doc in enumerate(docs[:3]):
        print(f"--- Document {i + 1} ---")
        print(f"ID:   {doc.id_}")
        print(f"Text: {doc.text[:120]}...")
        print("Metadata:")
        for k, v in doc.metadata.items():
            print(f"  {k}: {v}")
        print()
