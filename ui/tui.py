"""
KORE Terminal User Interface (TUI)

Built with Textual. 8-screen tabbed interface for managing
company knowledge extraction, review, and export.

Screens:
  1. Dashboard       — stats, charts, recent activity
  2. Extract         — query input, live extraction log, save
  3. Review          — needs_review queue with approve/reject/edit
  4. Rules           — all rules table with filter/detail/re-extract/delete
  5. Ingest          — source selection, local vs live, progress log
  6. Settings        — provider config, test connections, license info
  7. Knowledge Graph — entity browser, relationships, rule links
  8. Agent Memory    — memory entries, freshness, MCP controls
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Textual imports — wrapped so run.py can fail gracefully
try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, VerticalScroll
    from textual.reactive import reactive
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        RichLog,
        Static,
        TabbedContent,
        TabPane,
    )
    from textual.worker import Worker, get_current_worker
except ImportError as _textual_import_error:
    raise RuntimeError(
        "textual is not installed. Run: pip install textual"
    ) from _textual_import_error

from output.schema import BusinessRule
from output.writer import load_rules, update_rule
from output.skills_writer import export_skills_json
from engine.graph import run_extraction
from engine.reextractor import reextract_rule
from storage.vector_store import get_chunk_count, get_chunk_stats, health_check

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_all_rules() -> list[BusinessRule]:
    try:
        return load_rules()
    except Exception:
        return []


def _get_provider_info() -> dict[str, str]:
    try:
        from providers.config import get_config
        cfg = get_config()
        return {
            "llm_provider": cfg.llm_provider,
            "llm_model": cfg.llm_model,
            "embedding_provider": cfg.embedding_provider,
            "embedding_model": cfg.embedding_model,
            "vector_store_backend": cfg.vector_store_backend,
            "lancedb_data_dir": cfg.lancedb_data_dir,
            "pgvector_url": cfg.pgvector_url or "",
            "llm_temperature": str(cfg.llm_temperature),
            "embedding_dimensions": str(cfg.embedding_dimensions),
            "deployment_mode": cfg.deployment_mode,
        }
    except Exception:
        return {}


def _get_mcp_status() -> dict[str, Any]:
    """Return MCP server status by checking if the port is open."""
    import socket
    host, port = "127.0.0.1", 3333
    try:
        with socket.create_connection((host, port), timeout=1):
            return {"running": True, "port": port}
    except OSError:
        return {"running": False, "port": None}


def _get_license_info() -> dict[str, Any]:
    """Return license info. Returns placeholder if module not built yet."""
    try:
        from licensing.license import get_license_info
        return get_license_info()
    except Exception:
        return {
            "customer": "Trial",
            "expiry": None,
            "features": ["extraction", "review", "export"],
            "days_remaining": 14,
            "status": "trial",
        }


def _load_graph() -> "KnowledgeGraph":
    """Load the knowledge graph from disk."""
    try:
        from graph.store import load_graph
        return load_graph("graph/knowledge_graph.json")
    except Exception:
        from graph.models import KnowledgeGraph
        return KnowledgeGraph()


def _get_graph_entities() -> list[dict[str, Any]]:
    """Return knowledge graph entities as plain dicts for the TUI."""
    graph = _load_graph()
    entities = []
    for ent in graph.entities.values():
        entities.append({
            "id": ent.entity_id,
            "name": ent.name,
            "type": ent.entity_type,
            "description": ent.description or "",
            "mention_count": ent.mention_count,
            "last_seen": ent.last_seen.isoformat() if ent.last_seen else "",
            "source_rule_ids": ent.source_rule_ids,
        })
    return entities


def _get_memory_entries() -> list[dict[str, Any]]:
    """Return memory store entries. Falls back to demo data."""
    path = Path("memory/store.json")
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries", [])
            # Map Pydantic fields to TUI expected keys
            for e in entries:
                if "last_updated" in e and "last_accessed" not in e:
                    e["last_accessed"] = e["last_updated"][:10] if isinstance(e["last_updated"], str) else "?"
                if "freshness" not in e:
                    e["freshness"] = 1.0
            return entries
        except Exception:
            pass
    # Demo data
    return [
        {"id": "mem_1", "type": "rule_summary", "content": "Refund within 7 days", "freshness": 0.95, "access_count": 12, "last_accessed": "2024-06-01"},
        {"id": "mem_2", "type": "escalation_path", "content": "Escalate to #ops after 30m", "freshness": 0.82, "access_count": 5, "last_accessed": "2024-05-20"},
        {"id": "mem_3", "type": "compliance_check", "content": "GDPR data retention 90d", "freshness": 0.45, "access_count": 2, "last_accessed": "2024-03-01"},
    ]


# ---------------------------------------------------------------------------
# Shared widgets
# ---------------------------------------------------------------------------

class StatCard(Static):
    """A simple stat card: label on top, value below."""

    def __init__(self, label: str, value: str = "—", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._label = label
        self._value = value

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="stat-label")
        yield Label(self._value, classes="stat-value")

    def set_value(self, value: str) -> None:
        self._value = value
        label = self.query_one(".stat-value", Label)
        label.update(value)


# ---------------------------------------------------------------------------
# Screen 1 — Dashboard
# ---------------------------------------------------------------------------

class DashboardScreen(Screen):
    """Overview screen with stats, charts, recent activity."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
    ]

    CSS = """
    DashboardScreen {
        padding: 1;
    }
    .stat-row {
        height: auto;
        margin: 1 0;
    }
    StatCard {
        width: 1fr;
        height: auto;
        padding: 1;
        border: solid $primary;
        text-align: center;
    }
    .stat-label {
        text-style: bold;
        color: $text-muted;
    }
    .stat-value {
        text-style: bold;
        color: $primary;
        text-align: center;
    }
    .section-title {
        text-style: bold underline;
        margin: 1 0;
    }
    .recent-item {
        padding: 0 1;
    }
    .backend-info {
        color: $text-muted;
        margin: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("KORE Dashboard", classes="section-title")

        with Horizontal(classes="stat-row"):
            yield StatCard("Total Chunks", "—", id="dash-chunks")
            yield StatCard("Total Rules", "—", id="dash-rules")
            yield StatCard("Verified", "—", id="dash-verified")
            yield StatCard("Needs Review", "—", id="dash-needs-review")
            yield StatCard("Entities", "—", id="dash-entities")

        yield Static("Vector Store", classes="section-title")
        yield Static("—", id="dash-backend", classes="backend-info")

        yield Static("Top Entities", classes="section-title")
        yield Static("—", id="dash-top-entities")

        yield Static("Rules by Category", classes="section-title")
        yield Static("—", id="dash-categories")

        yield Static("Recent Extractions", classes="section-title")
        yield Static("—", id="dash-recent")

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        try:
            chunk_count = get_chunk_count()
        except Exception:
            chunk_count = 0
        self.query_one("#dash-chunks", StatCard).set_value(str(chunk_count))

        rules = _load_all_rules()
        total = len(rules)
        verified = sum(1 for r in rules if r.verification_status == "verified")
        needs_review = sum(1 for r in rules if r.verification_status == "needs_review")

        self.query_one("#dash-rules", StatCard).set_value(str(total))
        self.query_one("#dash-verified", StatCard).set_value(str(verified))
        self.query_one("#dash-needs-review", StatCard).set_value(str(needs_review))

        # Graph stats
        graph = _load_graph()
        self.query_one("#dash-entities", StatCard).set_value(str(len(graph.entities)))

        from graph.store import get_most_connected_entities
        top_ents = get_most_connected_entities(graph, top_n=3)
        if top_ents:
            lines = []
            for ent, degree in top_ents:
                lines.append(f"  {ent.name} ({ent.entity_type}) — {degree} connections")
            self.query_one("#dash-top-entities", Static).update("\n".join(lines))
        else:
            self.query_one("#dash-top-entities", Static).update("  [dim]No graph entities yet.[/dim]")

        info = _get_provider_info()
        if info:
            backend = info.get("vector_store_backend", "unknown")
            if backend == "lancedb":
                path = os.path.expanduser(info.get("lancedb_data_dir", "~/.kore/data"))
                backend_str = f"Backend: LanceDB  |  Path: {path}  |  Chunks: {chunk_count}"
            else:
                backend_str = f"Backend: pgvector  |  Chunks: {chunk_count}"
            provider_str = f"LLM: {info.get('llm_provider', '?')} / {info.get('llm_model', '?')}"
            self.query_one("#dash-backend", Static).update(f"{provider_str}\n{backend_str}")
        else:
            self.query_one("#dash-backend", Static).update(
                "[yellow]No provider.config.yaml found. Run setup first.[/yellow]"
            )

        from collections import Counter
        cat_counts = Counter(r.rule_category for r in rules)
        if cat_counts:
            lines = [f"  {cat}: {cnt}" for cat, cnt in sorted(cat_counts.items())]
            self.query_one("#dash-categories", Static).update("\n".join(lines))
        else:
            self.query_one("#dash-categories", Static).update("  [dim]No rules extracted yet.[/dim]")

        recent = sorted(
            [r for r in rules if r.created_at],
            key=lambda r: r.created_at,
            reverse=True,
        )[:5]
        if recent:
            lines = []
            for r in recent:
                ts = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
                status_color = {
                    "verified": "green",
                    "needs_review": "yellow",
                    "rejected": "red",
                    "parse_failed": "red",
                }.get(r.verification_status, "white")
                excerpt = r.rule_text[:50] + "..." if len(r.rule_text) > 50 else r.rule_text
                lines.append(
                    f"  [{status_color}]{r.verification_status:12}[/{status_color}] "
                    f"{ts}  {excerpt}"
                )
            self.query_one("#dash-recent", Static).update("\n".join(lines))
        else:
            self.query_one("#dash-recent", Static).update("  [dim]No extractions yet.[/dim]")


# ---------------------------------------------------------------------------
# Screen 2 — Extract
# ---------------------------------------------------------------------------

class ExtractScreen(Screen):
    """Run extraction pipeline with live log output."""

    CSS = """
    ExtractScreen {
        padding: 1;
    }
    .section-title {
        text-style: bold underline;
        margin: 1 0;
    }
    #extract-input {
        margin: 1 0;
    }
    #extract-log {
        border: solid $primary;
        height: 1fr;
        padding: 0 1;
    }
    #extract-result {
        height: auto;
        border: solid $success;
        padding: 1;
        display: none;
    }
    .result-label {
        text-style: bold;
        color: $primary;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Extract Business Rule", classes="section-title")
        yield Input(placeholder="Enter your query (e.g. 'refund policy')", id="extract-input")
        yield RichLog(id="extract-log", wrap=True)
        with Vertical(id="extract-result"):
            yield Static("Result", classes="result-label")
            yield Static("—", id="extract-result-text")
            yield Static("—", id="extract-result-meta")
            with Horizontal():
                yield Button("Save Rule", id="extract-save", variant="success")
                yield Button("Discard", id="extract-discard", variant="error")

    def on_mount(self) -> None:
        self.query_one("#extract-input", Input).focus()
        self._final_state: dict[str, Any] | None = None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "extract-input":
            self._run_extraction(event.value)

    def _run_extraction(self, query: str) -> None:
        log = self.query_one("#extract-log", RichLog)
        result_box = self.query_one("#extract-result", Vertical)
        result_box.styles.display = "none"
        log.clear()
        log.write("[bold cyan]Starting extraction...[/bold cyan]")
        log.write(f"Query: {query}")

        self._final_state = None
        self._extraction_query = query

        self.run_worker(self._do_extraction(query), exclusive=True)

    def _do_extraction(self, query: str) -> None:
        log = self.query_one("#extract-log", RichLog)
        log.write("[dim]Retrieving chunks...[/dim]")
        start = time.time()

        try:
            final_state = run_extraction(query)
        except Exception as exc:
            self.call_from_thread(log.write, f"[bold red]Extraction failed: {exc}[/bold red]")
            return

        elapsed = time.time() - start
        self._final_state = final_state

        status = final_state.get("verification_status", "unknown")
        confidence = final_state.get("rule_confidence", 0.0)
        repairs = final_state.get("json_repair_attempts", 0)
        rule_text = final_state.get("candidate_rule", "")
        source_count = len(final_state.get("source_refs", []))

        status_color = {
            "verified": "green",
            "needs_review": "yellow",
            "rejected": "red",
            "parse_failed": "red",
        }.get(status, "white")

        log.write("")
        log.write(f"[bold {status_color}]Status: {status}[/bold {status_color}]")
        log.write(f"Confidence: {confidence:.2f}")
        log.write(f"Sources: {source_count}")
        log.write(f"JSON repairs: {repairs}")
        log.write(f"Time: {elapsed:.1f}s")

        if repairs > 0:
            log.write(f"[yellow]⚠ JSON repair used ({repairs} attempt(s))[/yellow]")

        if status == "rejected":
            reason = final_state.get("rejection_reason", "unknown")
            log.write(f"[dim]Rejection reason: {reason}[/dim]")

        self.call_from_thread(self._show_result, rule_text, status, confidence, source_count, elapsed)

    def _show_result(
        self,
        rule_text: str,
        status: str,
        confidence: float,
        source_count: int,
        elapsed: float,
    ) -> None:
        result_box = self.query_one("#extract-result", Vertical)
        result_text = self.query_one("#extract-result-text", Static)
        result_meta = self.query_one("#extract-result-meta", Static)

        result_text.update(rule_text or "(no rule extracted)")
        status_color = {
            "verified": "green",
            "needs_review": "yellow",
            "rejected": "red",
            "parse_failed": "red",
        }.get(status, "white")
        result_meta.update(
            f"Status: [{status_color}]{status}[/{status_color}]  |  "
            f"Confidence: {confidence:.2f}  |  "
            f"Sources: {source_count}  |  "
            f"Time: {elapsed:.1f}s"
        )
        result_box.styles.display = "block"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "extract-save":
            self._save_rule()
        elif event.button.id == "extract-discard":
            self._discard_rule()

    def _save_rule(self) -> None:
        if not self._final_state or not self._final_state.get("final_rule"):
            self.query_one("#extract-log", RichLog).write("[red]Nothing to save.[/red]")
            return

        try:
            rule = BusinessRule(**self._final_state["final_rule"])
            from output.writer import save_rule
            path = save_rule(rule)
            log = self.query_one("#extract-log", RichLog)
            log.write(f"[green]✓ Saved to {path}[/green]")

            from engine.contradiction_detector import find_contradictions, save_contradiction_report
            existing = _load_all_rules()
            reports = find_contradictions(rule, existing)
            for report in reports:
                save_contradiction_report(report)
                log.write(
                    f"[bold red]⚠ Contradiction with {report.rule_id_b[:8]}: "
                    f"{report.contradiction_description}[/bold red]"
                )

            dash = self.app.query_one("#screen-dashboard", DashboardScreen)
            dash.action_refresh()
        except Exception as exc:
            self.query_one("#extract-log", RichLog).write(f"[red]Save failed: {exc}[/red]")

    def _discard_rule(self) -> None:
        self.query_one("#extract-result", Vertical).styles.display = "none"
        self.query_one("#extract-log", RichLog).write("[dim]Discarded.[/dim]")
        self._final_state = None


# ---------------------------------------------------------------------------
# Screen 3 — Review Queue
# ---------------------------------------------------------------------------

class ReviewScreen(Screen):
    """Interactive approval queue for needs_review rules."""

    BINDINGS = [
        Binding("a", "approve", "Approve", show=False),
        Binding("r", "reject", "Reject", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("s", "skip", "Skip", show=False),
        Binding("enter", "expand", "Expand", show=False),
    ]

    CSS = """
    ReviewScreen {
        padding: 1;
    }
    .section-title {
        text-style: bold underline;
        margin: 1 0;
    }
    #review-table {
        height: 1fr;
        border: solid $primary;
    }
    #review-detail {
        height: auto;
        border: solid $primary-lighten-2;
        padding: 1;
        display: none;
    }
    .detail-label {
        text-style: bold;
        color: $primary;
    }
    .action-bar {
        height: auto;
        margin: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Review Queue", classes="section-title")
        yield Static("Use arrow keys to navigate. Enter to expand. [A]pprove [R]eject [E]dit [S]kip", classes="hint")
        yield DataTable(id="review-table")
        with Vertical(id="review-detail"):
            yield Static("Rule Detail", classes="detail-label")
            yield Static("—", id="review-detail-text")
            yield Static("—", id="review-detail-meta")
        with Horizontal(classes="action-bar"):
            yield Button("Approve (a)", id="review-approve", variant="success")
            yield Button("Reject (r)", id="review-reject", variant="error")
            yield Button("Edit (e)", id="review-edit")
            yield Button("Skip (s)", id="review-skip")

    def on_mount(self) -> None:
        self._rules: list[BusinessRule] = []
        self._needs_review: list[BusinessRule] = []
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#review-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Rule", "Category", "Confidence", "Version")

        self._rules = _load_all_rules()
        self._needs_review = [r for r in self._rules if r.verification_status == "needs_review"]

        if not self._needs_review:
            table.add_row("—", "No rules awaiting review.", "—", "—", "—")
            self.query_one("#review-detail", Vertical).styles.display = "none"
            return

        for i, rule in enumerate(self._needs_review, 1):
            excerpt = rule.rule_text[:50] + "..." if len(rule.rule_text) > 50 else rule.rule_text
            table.add_row(
                str(i),
                excerpt,
                rule.rule_category,
                f"{rule.confidence:.2f}",
                str(rule.version),
                key=rule.rule_id,
            )

    def action_expand(self) -> None:
        table = self.query_one("#review-table", DataTable)
        row_key = table.cursor_row
        if row_key is None or not self._needs_review:
            return
        try:
            idx = int(row_key)
            if 0 <= idx < len(self._needs_review):
                rule = self._needs_review[idx]
                self._show_detail(rule)
        except (ValueError, IndexError):
            pass

    def _show_detail(self, rule: BusinessRule) -> None:
        detail = self.query_one("#review-detail", Vertical)
        text = self.query_one("#review-detail-text", Static)
        meta = self.query_one("#review-detail-meta", Static)

        text.update(rule.rule_text)
        sources = " | ".join(
            f"{ref.source_type}:{ref.channel}" for ref in rule.source_refs
        )
        notes = rule.ambiguity_notes or "None"
        meta.update(
            f"Category: {rule.rule_category}  |  "
            f"Confidence: {rule.confidence:.2f}  |  "
            f"Version: {rule.version}\n"
            f"Sources: {sources}\n"
            f"Ambiguity: {notes}"
        )
        detail.styles.display = "block"
        self._current_rule = rule

    def _current_rule_index(self) -> int | None:
        table = self.query_one("#review-table", DataTable)
        row = table.cursor_row
        if row is None:
            return None
        try:
            idx = int(row)
            if 0 <= idx < len(self._needs_review):
                return idx
        except (ValueError, IndexError):
            pass
        return None

    def _get_current_rule(self) -> BusinessRule | None:
        idx = self._current_rule_index()
        if idx is not None:
            return self._needs_review[idx]
        return None

    def action_approve(self) -> None:
        self._act("approve")

    def action_reject(self) -> None:
        self._act("reject")

    def action_edit(self) -> None:
        self._act("edit")

    def action_skip(self) -> None:
        self._act("skip")

    def _act(self, action: str) -> None:
        rule = self._get_current_rule()
        if rule is None:
            return

        if action == "approve":
            try:
                update_rule(rule.rule_id, {"verification_status": "verified", "approved_by": "tui_review"})
                self.notify(f"Approved rule {rule.rule_id[:8]}", severity="information")
            except Exception as exc:
                self.notify(f"Approve failed: {exc}", severity="error")
        elif action == "reject":
            try:
                update_rule(rule.rule_id, {"verification_status": "rejected", "ambiguity_notes": "Rejected via TUI"})
                self.notify(f"Rejected rule {rule.rule_id[:8]}", severity="warning")
            except Exception as exc:
                self.notify(f"Reject failed: {exc}", severity="error")
        elif action == "edit":
            try:
                update_rule(rule.rule_id, {"ambiguity_notes": "Edited via TUI (placeholder)"})
                self.notify(f"Edited rule {rule.rule_id[:8]}", severity="information")
            except Exception as exc:
                self.notify(f"Edit failed: {exc}", severity="error")
        elif action == "skip":
            self.notify("Skipped", severity="information")

        self._refresh_table()
        dash = self.app.query_one("#screen-dashboard", DashboardScreen)
        dash.action_refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "review-approve": "approve",
            "review-reject": "reject",
            "review-edit": "edit",
            "review-skip": "skip",
        }
        action = mapping.get(event.button.id)
        if action:
            self._act(action)


# ---------------------------------------------------------------------------
# Screen 4 — All Rules
# ---------------------------------------------------------------------------

class RulesScreen(Screen):
    """Browse all rules with filtering, detail view, re-extract, delete."""

    BINDINGS = [
        Binding("f", "filter", "Filter", show=False),
        Binding("enter", "detail", "Detail", show=False),
        Binding("x", "reextract", "Re-extract", show=False),
        Binding("d", "delete", "Delete", show=False),
    ]

    CSS = """
    RulesScreen {
        padding: 1;
    }
    .section-title {
        text-style: bold underline;
        margin: 1 0;
    }
    #rules-filter {
        margin: 1 0;
        display: none;
    }
    #rules-table {
        height: 1fr;
        border: solid $primary;
    }
    #rules-detail {
        height: auto;
        border: solid $primary-lighten-2;
        padding: 1;
        display: none;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("All Rules", classes="section-title")
        yield Static("Enter=detail  X=re-extract  D=delete  F=filter", classes="hint")
        with Horizontal(id="rules-filter"):
            yield Input(placeholder="Filter by status (verified/needs_review/rejected)", id="filter-status")
            yield Input(placeholder="Filter by category", id="filter-category")
            yield Button("Apply", id="filter-apply", variant="primary")
            yield Button("Clear", id="filter-clear")
        yield DataTable(id="rules-table")
        with Vertical(id="rules-detail"):
            yield Static("Rule Detail", classes="detail-label")
            yield Static("—", id="rules-detail-text")
            yield Static("—", id="rules-detail-meta")

    def on_mount(self) -> None:
        self._all_rules: list[BusinessRule] = []
        self._filtered: list[BusinessRule] = []
        self._refresh_table()

    def _refresh_table(self, status_filter: str = "", category_filter: str = "") -> None:
        table = self.query_one("#rules-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Rule", "Category", "Status", "Confidence", "Version", "Approved By")

        self._all_rules = _load_all_rules()
        self._filtered = self._all_rules[:]

        if status_filter:
            self._filtered = [r for r in self._filtered if r.verification_status == status_filter]
        if category_filter:
            self._filtered = [r for r in self._filtered if r.rule_category == category_filter]

        if not self._filtered:
            table.add_row("No rules match filters.", "—", "—", "—", "—", "—")
            return

        for rule in self._filtered:
            excerpt = rule.rule_text[:45] + "..." if len(rule.rule_text) > 45 else rule.rule_text
            status_color = {
                "verified": "green",
                "needs_review": "yellow",
                "rejected": "red",
                "parse_failed": "red",
            }.get(rule.verification_status, "white")
            table.add_row(
                excerpt,
                rule.rule_category,
                f"[{status_color}]{rule.verification_status}[/{status_color}]",
                f"{rule.confidence:.2f}",
                str(rule.version),
                rule.approved_by or "—",
                key=rule.rule_id,
            )

    def action_filter(self) -> None:
        filt = self.query_one("#rules-filter", Horizontal)
        filt.styles.display = "block"
        self.query_one("#filter-status", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "filter-apply":
            status = self.query_one("#filter-status", Input).value.strip()
            category = self.query_one("#filter-category", Input).value.strip()
            self._refresh_table(status_filter=status, category_filter=category)
        elif event.button.id == "filter-clear":
            self.query_one("#filter-status", Input).value = ""
            self.query_one("#filter-category", Input).value = ""
            self.query_one("#rules-filter", Horizontal).styles.display = "none"
            self._refresh_table()

    def action_detail(self) -> None:
        rule = self._get_current_rule()
        if rule is None:
            return
        detail = self.query_one("#rules-detail", Vertical)
        text = self.query_one("#rules-detail-text", Static)
        meta = self.query_one("#rules-detail-meta", Static)

        text.update(rule.rule_text)
        sources = " | ".join(
            f"{ref.source_type}:{ref.channel}" for ref in rule.source_refs
        )
        meta.update(
            f"ID: {rule.rule_id}\n"
            f"Category: {rule.rule_category}  |  "
            f"Confidence: {rule.confidence:.2f}  |  "
            f"Status: {rule.verification_status}  |  "
            f"Version: {rule.version}\n"
            f"Approved By: {rule.approved_by or '—'}\n"
            f"Sources: {sources}\n"
            f"Ambiguity: {rule.ambiguity_notes or 'None'}"
        )
        detail.styles.display = "block"

    def action_reextract(self) -> None:
        rule = self._get_current_rule()
        if rule is None:
            return
        self.app.push_screen(ReextractConfirmScreen(rule.rule_id))

    def action_delete(self) -> None:
        rule = self._get_current_rule()
        if rule is None:
            return
        self.app.push_screen(DeleteConfirmScreen(rule.rule_id))

    def _get_current_rule(self) -> BusinessRule | None:
        table = self.query_one("#rules-table", DataTable)
        row = table.cursor_row
        if row is None:
            return None
        try:
            idx = int(row)
            if 0 <= idx < len(self._filtered):
                return self._filtered[idx]
        except (ValueError, IndexError):
            pass
        return None


# ---------------------------------------------------------------------------
# Screen 5 — Ingest
# ---------------------------------------------------------------------------

class IngestScreen(Screen):
    """Ingest data from sources with progress logging."""

    CSS = """
    IngestScreen {
        padding: 1;
    }
    .section-title {
        text-style: bold underline;
        margin: 1 0;
    }
    #ingest-source-row {
        height: auto;
        margin: 1 0;
    }
    #ingest-mode-row {
        height: auto;
        margin: 1 0;
    }
    #ingest-path {
        margin: 1 0;
        display: none;
    }
    #ingest-creds {
        margin: 1 0;
        display: none;
        color: $text-muted;
    }
    #ingest-log {
        border: solid $primary;
        height: 1fr;
        padding: 0 1;
    }
    #ingest-summary {
        height: auto;
        border: solid $success;
        padding: 1;
        display: none;
    }
    """

    SOURCES = {
        "slack": "Slack",
        "github": "GitHub",
        "notion": "Notion",
        "jira": "Jira",
    }

    def compose(self) -> ComposeResult:
        yield Static("Ingest Data", classes="section-title")

        yield Static("Source", classes="label")
        with Horizontal(id="ingest-source-row"):
            yield Button("Slack", id="src-slack", variant="primary")
            yield Button("GitHub", id="src-github")
            yield Button("Notion", id="src-notion")
            yield Button("Jira", id="src-jira")

        yield Static("Mode", classes="label")
        with Horizontal(id="ingest-mode-row"):
            yield Button("Local Export", id="mode-local", variant="primary")
            yield Button("Live API", id="mode-live")

        yield Input(placeholder="Path to export directory", id="ingest-path")
        yield Static("—", id="ingest-creds")
        yield Button("Start Ingestion", id="ingest-start", variant="success")
        yield RichLog(id="ingest-log", wrap=True)
        with Vertical(id="ingest-summary"):
            yield Static("Summary", classes="result-label")
            yield Static("—", id="ingest-summary-text")

    def on_mount(self) -> None:
        self._source = "slack"
        self._mode = "local"
        self._update_source_ui()
        self._update_mode_ui()

    def _update_source_ui(self) -> None:
        for src in self.SOURCES:
            btn = self.query_one(f"#src-{src}", Button)
            btn.variant = "primary" if src == self._source else "default"
        if self._mode == "live":
            self._show_credential_status()

    def _update_mode_ui(self) -> None:
        path_input = self.query_one("#ingest-path", Input)
        creds_static = self.query_one("#ingest-creds", Static)
        local_btn = self.query_one("#mode-local", Button)
        live_btn = self.query_one("#mode-live", Button)

        if self._mode == "local":
            path_input.styles.display = "block"
            creds_static.styles.display = "none"
            local_btn.variant = "primary"
            live_btn.variant = "default"
        else:
            path_input.styles.display = "none"
            creds_static.styles.display = "block"
            local_btn.variant = "default"
            live_btn.variant = "primary"
            self._show_credential_status()

    def _show_credential_status(self) -> None:
        creds = self.query_one("#ingest-creds", Static)
        env_map = {
            "slack": ("SLACK_BOT_TOKEN",),
            "github": ("GITHUB_TOKEN",),
            "notion": ("NOTION_TOKEN",),
            "jira": ("JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN"),
        }
        keys = env_map.get(self._source, ())
        status = []
        for k in keys:
            val = os.getenv(k, "")
            if val:
                status.append(f"[green]✓[/green] {k}")
            else:
                status.append(f"[red]✗[/red] {k} [dim](not set)[/dim]")
        creds.update("Credentials:\n" + "\n".join(status) if status else "No credentials required.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid and bid.startswith("src-"):
            self._source = bid.replace("src-", "")
            self._update_source_ui()
        elif bid == "mode-local":
            self._mode = "local"
            self._update_mode_ui()
        elif bid == "mode-live":
            self._mode = "live"
            self._update_mode_ui()
        elif bid == "ingest-start":
            self._start_ingest()

    def _start_ingest(self) -> None:
        log = self.query_one("#ingest-log", RichLog)
        summary = self.query_one("#ingest-summary", Vertical)
        summary.styles.display = "none"
        log.clear()

        source = self._source
        if self._mode == "local":
            path = self.query_one("#ingest-path", Input).value.strip()
            if not path:
                log.write("[red]Path is required for local export mode.[/red]")
                return
            self.run_worker(self._do_ingest(source, path=path), exclusive=True)
        else:
            self.run_worker(self._do_ingest(source, live=True), exclusive=True)

    def _do_ingest(self, source: str, path: str | None = None, live: bool = False) -> None:
        log = self.query_one("#ingest-log", RichLog)
        log.write(f"[bold cyan]Ingesting {source} ({'live' if live else 'local'})...[/bold cyan]")

        try:
            from ingestion.loader import load
            from ingestion.chunker import chunk_documents
            from storage.vector_store import init_db, store_chunks

            start = time.time()
            if live:
                docs = load(source, live=True)
            else:
                docs = load(source, path=path)
            log.write(f"Loaded {len(docs)} documents")

            chunks = chunk_documents(docs)
            log.write(f"Created {len(chunks)} chunks")

            init_db()
            stored = store_chunks(chunks)
            elapsed = time.time() - start
            log.write(f"[green]✓ Stored {stored} chunks in {elapsed:.1f}s[/green]")

            self.call_from_thread(self._show_summary, len(docs), len(chunks), stored, elapsed)

            # Refresh dashboard
            dash = self.app.query_one("#screen-dashboard", DashboardScreen)
            self.call_from_thread(dash.action_refresh)
        except Exception as exc:
            self.call_from_thread(log.write, f"[bold red]Ingest failed: {exc}[/bold red]")

    def _show_summary(self, docs: int, chunks: int, stored: int, elapsed: float) -> None:
        summary = self.query_one("#ingest-summary", Vertical)
        text = self.query_one("#ingest-summary-text", Static)
        text.update(
            f"Documents: {docs}\n"
            f"Chunks: {chunks}\n"
            f"Stored: {stored}\n"
            f"Time: {elapsed:.1f}s"
        )
        summary.styles.display = "block"


# ---------------------------------------------------------------------------
# Screen 6 — Settings
# ---------------------------------------------------------------------------

class SettingsScreen(Screen):
    """Provider config, test connections, license info."""

    BINDINGS = [
        Binding("t", "test_all", "Test All", show=False),
    ]

    CSS = """
    SettingsScreen {
        padding: 1;
    }
    .section-title {
        text-style: bold underline;
        margin: 1 0;
    }
    .config-block {
        border: solid $primary;
        padding: 1;
        margin: 1 0;
        height: auto;
    }
    .config-label {
        text-style: bold;
        color: $primary;
    }
    .config-value {
        color: $text;
    }
    .license-block {
        border: solid $warning;
        padding: 1;
        margin: 1 0;
        height: auto;
    }
    .license-active {
        color: $success;
        text-style: bold;
    }
    .license-trial {
        color: $warning;
        text-style: bold;
    }
    .license-expired {
        color: $error;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Settings", classes="section-title")

        with Horizontal():
            yield Button("Test LLM", id="test-llm")
            yield Button("Test Embedding", id="test-embedding")
            yield Button("Test Vector Store", id="test-vector")
            yield Button("Reconfigure", id="reconfigure", variant="warning")

        yield Static("Provider Configuration", classes="section-title")
        with Vertical(id="config-block", classes="config-block"):
            yield Static("—", id="config-text")

        yield Static("License", classes="section-title")
        with Vertical(id="license-block", classes="license-block"):
            yield Static("—", id="license-text")

        yield Static("Test Results", classes="section-title")
        yield Static("—", id="test-results")

    def on_mount(self) -> None:
        self._refresh_config()
        self._refresh_license()

    def _refresh_config(self) -> None:
        info = _get_provider_info()
        text = self.query_one("#config-text", Static)
        if info:
            lines = [
                f"[b]LLM:[/b]          {info.get('llm_provider', '?')} / {info.get('llm_model', '?')}",
                f"[b]Embedding:[/b]    {info.get('embedding_provider', '?')} / {info.get('embedding_model', '?')}",
                f"[b]Dimensions:[/b]   {info.get('embedding_dimensions', '?')}",
                f"[b]Mode:[/b]         {info.get('deployment_mode', '?')}",
                f"[b]Vector Store:[/b] {info.get('vector_store_backend', '?')}",
            ]
            if info.get("vector_store_backend") == "lancedb":
                lines.append(f"[b]Data Dir:[/b]     {info.get('lancedb_data_dir', '')}")
            text.update("\n".join(lines))
        else:
            text.update("[yellow]No provider.config.yaml found. Run setup first.[/yellow]")

    def _refresh_license(self) -> None:
        lic = _get_license_info()
        text = self.query_one("#license-text", Static)
        status = lic.get("status", "trial")
        days = lic.get("days_remaining", 0)

        if status == "active":
            style = "license-active"
            status_str = "Active"
        elif status == "trial":
            style = "license-trial"
            status_str = f"Trial ({days} days left)"
        else:
            style = "license-expired"
            status_str = "Expired"

        features = ", ".join(lic.get("features", []))
        lines = [
            f"Customer: {lic.get('customer', 'Unknown')}",
            f"Status: [{style}]{status_str}[/{style}]",
            f"Features: {features}",
        ]
        if lic.get("expiry"):
            lines.append(f"Expires: {lic['expiry']}")
        text.update("\n".join(lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        results = self.query_one("#test-results", Static)

        if bid == "test-llm":
            results.update("[dim]Testing LLM connection...[/dim]")
            self.run_worker(self._test_llm(), exclusive=True)
        elif bid == "test-embedding":
            results.update("[dim]Testing embedding connection...[/dim]")
            self.run_worker(self._test_embedding(), exclusive=True)
        elif bid == "test-vector":
            results.update("[dim]Testing vector store...[/dim]")
            self.run_worker(self._test_vector(), exclusive=True)
        elif bid == "reconfigure":
            results.update("[yellow]Reconfigure: run 'python run.py setup' from your shell.[/yellow]")

    def _test_llm(self) -> None:
        try:
            from providers.llm import test_connection
            ok = test_connection()
            msg = "[green]✓ LLM connection OK[/green]" if ok else "[red]✗ LLM connection failed[/red]"
            self.call_from_thread(self.query_one("#test-results", Static).update, msg)
        except Exception as exc:
            self.call_from_thread(self.query_one("#test-results", Static).update, f"[red]Error: {exc}[/red]")

    def _test_embedding(self) -> None:
        try:
            from providers.embedder import embed_text
            from providers.config import get_config
            vec = embed_text("hello", config=get_config())
            dims = len(vec)
            msg = f"[green]✓ Embedding OK ({dims} dimensions)[/green]"
            self.call_from_thread(self.query_one("#test-results", Static).update, msg)
        except Exception as exc:
            self.call_from_thread(self.query_one("#test-results", Static).update, f"[red]Error: {exc}[/red]")

    def _test_vector(self) -> None:
        try:
            ok = health_check()
            count = get_chunk_count()
            msg = f"[green]✓ Vector store OK ({count} chunks)[/green]" if ok else "[red]✗ Vector store unhealthy[/red]"
            self.call_from_thread(self.query_one("#test-results", Static).update, msg)
        except Exception as exc:
            self.call_from_thread(self.query_one("#test-results", Static).update, f"[red]Error: {exc}[/red]")


# ---------------------------------------------------------------------------
# Screen 7 — Knowledge Graph
# ---------------------------------------------------------------------------

class KnowledgeGraphScreen(Screen):
    """Browse entities and relationships in the knowledge graph.

    Left panel (40%): searchable entity browser grouped by type.
    Right panel (60%): entity detail, connected rules, related entities, 2-hop.
    """

    BINDINGS = [
        Binding("slash", "search", "Search", show=False),
        Binding("r", "related", "Related Rules", show=False),
        Binding("g", "expand", "Expand Hop", show=False),
        Binding("escape", "esc", "Back", show=False),
    ]

    CSS = """
    KnowledgeGraphScreen {
        padding: 1;
    }
    .section-title {
        text-style: bold underline;
        margin: 1 0;
    }
    #kg-search {
        margin: 1 0;
        display: none;
    }
    #kg-layout {
        height: 1fr;
    }
    #kg-entities {
        width: 40%;
        border: solid $primary;
    }
    #kg-detail {
        width: 60%;
        border: solid $primary-lighten-2;
        padding: 1;
    }
    .detail-label {
        text-style: bold;
        color: $primary;
        margin: 1 0 0 0;
    }
    .detail-value {
        color: $text;
        margin: 0 0 1 0;
    }
    .rel-item {
        margin: 0 0 0 1;
    }
    .entity-type-header {
        text-style: bold;
        color: $primary-lighten-2;
        margin: 1 0 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Knowledge Graph", classes="section-title")
        yield Static("/=search  R=related rules  G=expand hop  Esc=back", classes="hint")
        yield Input(placeholder="Search entities...", id="kg-search-input")
        with Horizontal(id="kg-layout"):
            yield DataTable(id="kg-entities")
            with Vertical(id="kg-detail"):
                yield Static("Name", classes="detail-label")
                yield Static("—", id="kg-detail-name", classes="detail-value")
                yield Static("Type", classes="detail-label")
                yield Static("—", id="kg-detail-type", classes="detail-value")
                yield Static("Description", classes="detail-label")
                yield Static("—", id="kg-detail-desc", classes="detail-value")
                yield Static("Mentions / Last Seen", classes="detail-label")
                yield Static("—", id="kg-detail-meta", classes="detail-value")
                yield Static("Connected Rules", classes="detail-label")
                yield Static("—", id="kg-detail-rules", classes="detail-value")
                yield Static("Related Entities", classes="detail-label")
                yield Static("—", id="kg-detail-rel", classes="detail-value")
                yield Static("2-Hop Relationships", classes="detail-label")
                yield Static("—", id="kg-detail-2hop", classes="detail-value")

    def on_mount(self) -> None:
        self._graph = _load_graph()
        self._entities: list[dict[str, Any]] = []
        self._selected_entity_id: str | None = None
        self._refresh_entities()

    def _refresh_entities(self, query: str = "") -> None:
        self._graph = _load_graph()
        all_ents = _get_graph_entities()

        if query:
            q = query.lower()
            all_ents = [
                e for e in all_ents
                if q in e.get("name", "").lower() or q in e.get("type", "").lower()
            ]

        self._entities = all_ents
        table = self.query_one("#kg-entities", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Type", "Mentions")

        if not all_ents:
            table.add_row("No entities found.", "—", "—")
            return

        # Sort by type then name to create implicit grouping
        for ent in sorted(all_ents, key=lambda x: (x.get("type", ""), x.get("name", ""))):
            table.add_row(
                ent.get("name", "?"),
                ent.get("type", "?"),
                str(ent.get("mention_count", 0)),
                key=ent.get("id", ""),
            )

    def action_search(self) -> None:
        inp = self.query_one("#kg-search-input", Input)
        inp.styles.display = "block"
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "kg-search-input":
            self._refresh_entities(query=event.value)
            # Hide search after submitting
            event.input.styles.display = "none"

    def action_esc(self) -> None:
        inp = self.query_one("#kg-search-input", Input)
        inp.styles.display = "none"
        inp.value = ""
        self._refresh_entities()

    def action_related(self) -> None:
        if self._selected_entity_id is None:
            return
        self._show_connected_rules(self._selected_entity_id)

    def action_expand(self) -> None:
        if self._selected_entity_id is None:
            return
        self._show_2hop(self._selected_entity_id)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "kg-entities":
            row_key = event.row_key
            if row_key is None:
                return
            entity_id = str(row_key.value)
            self._select_entity(entity_id)

    def _select_entity(self, entity_id: str) -> None:
        self._selected_entity_id = entity_id
        graph = self._graph
        entity = graph.entities.get(entity_id)
        if entity is None:
            return

        self.query_one("#kg-detail-name", Static).update(entity.name)
        self.query_one("#kg-detail-type", Static).update(entity.entity_type)
        self.query_one("#kg-detail-desc", Static).update(entity.description or "[dim]No description.[/dim]")
        self.query_one("#kg-detail-meta", Static).update(
            f"Mentions: {entity.mention_count}  |  Last seen: {entity.last_seen.strftime('%Y-%m-%d') if entity.last_seen else '?'}")

        # Connected rules
        self._show_connected_rules(entity_id)

        # Related entities (1-hop)
        self._show_related(entity_id)

        # 2-hop
        self._show_2hop(entity_id)

    def _show_connected_rules(self, entity_id: str) -> None:
        from graph.store import get_rules_for_entity
        rules = get_rules_for_entity(entity_id, self._graph)
        static = self.query_one("#kg-detail-rules", Static)
        if rules:
            lines = [f"  {r.rule_text[:70]}..." if len(r.rule_text) > 70 else f"  {r.rule_text}" for r in rules[:5]]
            static.update("\n".join(lines))
        else:
            static.update("[dim]No connected rules.[/dim]")

    def _show_related(self, entity_id: str) -> None:
        from graph.store import get_related_entities
        related = get_related_entities(entity_id, self._graph, max_hops=1)
        static = self.query_one("#kg-detail-rel", Static)

        if not related:
            static.update("[dim]No direct relationships.[/dim]")
            return

        lines: list[str] = []
        # Find the actual relationship records for confidence
        for rel_ent in related:
            rels = [
                r for r in self._graph.relationships
                if (r.from_entity_id == entity_id and r.to_entity_id == rel_ent.entity_id)
                or (r.to_entity_id == entity_id and r.from_entity_id == rel_ent.entity_id)
            ]
            if rels:
                rel = rels[0]
                direction = "→" if rel.from_entity_id == entity_id else "←"
                lines.append(
                    f"  {direction} {rel_ent.name} ({rel_ent.entity_type})  "
                    f"[dim]{rel.relationship_type}  conf={rel.confidence:.2f}[/dim]"
                )
            else:
                lines.append(f"  — {rel_ent.name} ({rel_ent.entity_type})")
        static.update("\n".join(lines))

    def _show_2hop(self, entity_id: str) -> None:
        from graph.store import get_related_entities
        two_hop = get_related_entities(entity_id, self._graph, max_hops=2)
        # Remove 1-hop entities and self
        one_hop_ids = {e.entity_id for e in get_related_entities(entity_id, self._graph, max_hops=1)}
        two_hop = [e for e in two_hop if e.entity_id != entity_id and e.entity_id not in one_hop_ids]

        static = self.query_one("#kg-detail-2hop", Static)
        if not two_hop:
            static.update("[dim]No 2-hop relationships.[/dim]")
            return

        lines: list[str] = []
        for ent in two_hop[:10]:
            lines.append(f"  — {ent.name} ({ent.entity_type})")
        static.update("\n".join(lines))

    def _get_current_entity(self) -> dict[str, Any] | None:
        if self._selected_entity_id is None:
            return None
        for e in self._entities:
            if e.get("id") == self._selected_entity_id:
                return e
        return None


# ---------------------------------------------------------------------------
# Screen 8 — Agent Memory
# ---------------------------------------------------------------------------

class AgentMemoryScreen(Screen):
    """Browse agent memory, trigger builds, control MCP server."""

    BINDINGS = [
        Binding("b", "build", "Build Memory", show=False),
        Binding("d", "decay", "Decay Sweep", show=False),
        Binding("s", "mcp", "Start MCP", show=False),
    ]

    CSS = """
    AgentMemoryScreen {
        padding: 1;
    }
    .section-title {
        text-style: bold underline;
        margin: 1 0;
    }
    .stat-row {
        height: auto;
        margin: 1 0;
    }
    #mem-layout {
        height: 1fr;
    }
    #mem-entries {
        width: 45%;
        border: solid $primary;
    }
    #mem-detail {
        width: 55%;
        border: solid $primary-lighten-2;
        padding: 1;
    }
    .freshness-high {
        color: $success;
    }
    .freshness-mid {
        color: $warning;
    }
    .freshness-low {
        color: $error;
    }
    .action-bar {
        height: auto;
        margin: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Agent Memory", classes="section-title")

        with Horizontal(classes="stat-row"):
            yield StatCard("Total Entries", "—", id="mem-total")
            yield StatCard("Fresh", "—", id="mem-fresh")
            yield StatCard("Stale", "—", id="mem-stale")
            yield StatCard("Last Built", "—", id="mem-last-built")

        with Horizontal(id="mem-layout"):
            yield DataTable(id="mem-entries")
            with Vertical(id="mem-detail"):
                yield Static("Memory Entry", classes="detail-label")
                yield Static("—", id="mem-detail-content")
                yield Static("—", id="mem-detail-meta")
                yield Static("Freshness", classes="detail-label")
                yield Static("—", id="mem-detail-freshness")

        with Horizontal(classes="action-bar"):
            yield Button("Build Memory (b)", id="mem-build", variant="primary")
            yield Button("Decay Sweep (d)", id="mem-decay")
            yield Button("Start MCP Server (s)", id="mem-mcp")

    def on_mount(self) -> None:
        self._entries: list[dict[str, Any]] = []
        self._refresh_entries()
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        entries = _get_memory_entries()
        total = len(entries)
        fresh = sum(1 for e in entries if e.get("freshness", 0) > 0.7)
        stale = sum(1 for e in entries if e.get("freshness", 1) < 0.5)

        self.query_one("#mem-total", StatCard).set_value(str(total))
        self.query_one("#mem-fresh", StatCard).set_value(str(fresh))
        self.query_one("#mem-stale", StatCard).set_value(str(stale))

        # Last built from file mtime
        path = Path("memory/store.json")
        if path.exists():
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            self.query_one("#mem-last-built", StatCard).set_value(mtime.strftime("%Y-%m-%d %H:%M"))
        else:
            self.query_one("#mem-last-built", StatCard).set_value("Never")

    def _refresh_entries(self) -> None:
        table = self.query_one("#mem-entries", DataTable)
        table.clear(columns=True)
        table.add_columns("Type", "Content", "Freshness", "Accessed")

        self._entries = _get_memory_entries()
        if not self._entries:
            table.add_row("—", "No memory entries.", "—", "—")
            return

        for e in self._entries:
            freshness = e.get("freshness", 1.0)
            if freshness > 0.7:
                f_str = f"[green]{freshness:.0%}[/green]"
            elif freshness > 0.4:
                f_str = f"[yellow]{freshness:.0%}[/yellow]"
            else:
                f_str = f"[red]{freshness:.0%}[/red]"

            content = e.get("content", "")[:35] + "..." if len(e.get("content", "")) > 35 else e.get("content", "")
            last = e.get("last_accessed", "?")
            if isinstance(last, str) and len(last) > 10:
                last = last[:10]
            table.add_row(e.get("type", "?"), content, f_str, last, key=e.get("id", ""))

    def _get_current_entry(self) -> dict[str, Any] | None:
        table = self.query_one("#mem-entries", DataTable)
        row = table.cursor_row
        if row is None:
            return None
        try:
            idx = int(row)
            if 0 <= idx < len(self._entries):
                return self._entries[idx]
        except (ValueError, IndexError):
            pass
        return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "mem-entries":
            entry = self._get_current_entry()
            if entry:
                self.query_one("#mem-detail-content", Static).update(entry.get("content", "—"))
                self.query_one("#mem-detail-meta", Static).update(
                    f"Type: {entry.get('type', '?')}  |  "
                    f"Access Count: {entry.get('access_count', 0)}  |  "
                    f"Last Accessed: {entry.get('last_accessed', '?')}"
                )
                freshness = entry.get("freshness", 1.0)
                bar = "█" * int(freshness * 10) + "░" * (10 - int(freshness * 10))
                color = "green" if freshness > 0.7 else "yellow" if freshness > 0.4 else "red"
                self.query_one("#mem-detail-freshness", Static).update(
                    f"[{color}]{bar}[/{color}]  {freshness:.0%}"
                )

    def action_build(self) -> None:
        self.notify("Building memory store...", severity="information")
        self.run_worker(self._do_build_memory(), exclusive=True)

    def _do_build_memory(self) -> None:
        try:
            from memory.store import build_memory_store
            store = build_memory_store()
            self.call_from_thread(
                self.notify,
                f"Memory store built: {store.entry_count} entries",
                severity="information",
            )
            self.call_from_thread(self._refresh_stats)
            self.call_from_thread(self._refresh_entries)
        except Exception as exc:
            self.call_from_thread(
                self.notify,
                f"Build failed: {exc}",
                severity="error",
            )

    def action_decay(self) -> None:
        self.notify("Running decay sweep...", severity="information")
        self.run_worker(self._do_decay_sweep(), exclusive=True)

    def _do_decay_sweep(self) -> None:
        try:
            from memory.decay import decay_sweep, get_freshness_summary
            updated, store = decay_sweep()
            summary = get_freshness_summary(store)
            self.call_from_thread(
                self.notify,
                f"Decay complete. {updated} entries updated. "
                f"High: {summary['high']}  Mid: {summary['mid']}  Low: {summary['low']}",
                severity="information",
            )
            self.call_from_thread(self._refresh_stats)
            self.call_from_thread(self._refresh_entries)
        except Exception as exc:
            self.call_from_thread(
                self.notify,
                f"Decay failed: {exc}",
                severity="error",
            )

    def action_mcp(self) -> None:
        status = _get_mcp_status()
        if status["running"]:
            self.notify(f"MCP server already running on port {status['port']}", severity="information")
        else:
            self.notify("MCP server start not yet implemented.", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "mem-build": "build",
            "mem-decay": "decay",
            "mem-mcp": "mcp",
        }
        action = mapping.get(event.button.id)
        if action:
            getattr(self, f"action_{action}")()


# ---------------------------------------------------------------------------
# Modal screens — confirmations
# ---------------------------------------------------------------------------

class DeleteConfirmScreen(ModalScreen[bool]):
    """Confirm deletion of a rule."""

    def __init__(self, rule_id: str) -> None:
        super().__init__()
        self._rule_id = rule_id

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(f"Delete rule {self._rule_id[:8]}?", id="dialog-title")
            yield Static("This cannot be undone.", id="dialog-msg")
            with Horizontal():
                yield Button("Delete", id="dialog-confirm", variant="error")
                yield Button("Cancel", id="dialog-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)


class ReextractConfirmScreen(ModalScreen[bool]):
    """Confirm re-extraction of a rule."""

    def __init__(self, rule_id: str) -> None:
        super().__init__()
        self._rule_id = rule_id

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(f"Re-extract rule {self._rule_id[:8]}?", id="dialog-title")
            yield Static("This will run the full extraction pipeline again.", id="dialog-msg")
            with Horizontal():
                yield Button("Re-extract", id="dialog-confirm", variant="primary")
                yield Button("Cancel", id="dialog-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class KoreApp(App):
    """KORE Textual TUI Application."""

    TITLE = "KORE"
    SUB_TITLE = "On-Premise Context Engine"
    CSS = """
    #dialog {
        width: 60;
        height: auto;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }
    #dialog-title {
        text-style: bold;
        content-align: center middle;
        height: auto;
        margin: 1 0;
    }
    #dialog-msg {
        color: $text-muted;
        content-align: center middle;
        height: auto;
        margin: 1 0;
    }
    .hint {
        color: $text-muted;
        margin: 0 0 1 0;
    }
    #status-bar {
        height: auto;
        background: $surface;
        padding: 0 2;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "next_tab", "Next Screen"),
        Binding("shift+tab", "prev_tab", "Prev Screen"),
        Binding("?", "help", "Help"),
        Binding("escape", "esc", "Back"),
    ]

    TAB_NAMES = [
        "tab-dashboard",
        "tab-extract",
        "tab-review",
        "tab-rules",
        "tab-ingest",
        "tab-settings",
        "tab-graph",
        "tab-memory",
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield self._build_status_bar()
        with TabbedContent(id="main-tabs"):
            with TabPane("Dashboard", id="tab-dashboard"):
                yield DashboardScreen(id="screen-dashboard")
            with TabPane("Extract", id="tab-extract"):
                yield ExtractScreen(id="screen-extract")
            with TabPane("Review", id="tab-review"):
                yield ReviewScreen(id="screen-review")
            with TabPane("Rules", id="tab-rules"):
                yield RulesScreen(id="screen-rules")
            with TabPane("Ingest", id="tab-ingest"):
                yield IngestScreen(id="screen-ingest")
            with TabPane("Settings", id="tab-settings"):
                yield SettingsScreen(id="screen-settings")
            with TabPane("Graph", id="tab-graph"):
                yield KnowledgeGraphScreen(id="screen-graph")
            with TabPane("Memory", id="tab-memory"):
                yield AgentMemoryScreen(id="screen-memory")
        yield Footer()

    def _build_status_bar(self) -> Static:
        mcp = _get_mcp_status()
        if mcp["running"]:
            mcp_str = f"MCP [green]●[/green] :{mcp['port']}"
        else:
            mcp_str = "MCP [dim]○ offline[/dim]"
        info = _get_provider_info()
        provider = f"{info.get('llm_provider', '?')} / {info.get('llm_model', '?')}" if info else "Not configured"
        return Static(f"{provider}  |  {mcp_str}", id="status-bar")

    def on_mount(self) -> None:
        self._update_header()

    def _update_header(self) -> None:
        info = _get_provider_info()
        if info:
            self.sub_title = f"{info.get('llm_provider', '?')} / {info.get('llm_model', '?')}"
        else:
            self.sub_title = "Run setup to configure providers"

    def action_next_tab(self) -> None:
        tabs = self.query_one("#main-tabs", TabbedContent)
        active = tabs.active
        try:
            idx = self.TAB_NAMES.index(active)
            next_idx = (idx + 1) % len(self.TAB_NAMES)
            tabs.active = self.TAB_NAMES[next_idx]
        except ValueError:
            pass

    def action_prev_tab(self) -> None:
        tabs = self.query_one("#main-tabs", TabbedContent)
        active = tabs.active
        try:
            idx = self.TAB_NAMES.index(active)
            prev_idx = (idx - 1) % len(self.TAB_NAMES)
            tabs.active = self.TAB_NAMES[prev_idx]
        except ValueError:
            pass

    def action_help(self) -> None:
        self.notify(
            "Shortcuts: q=quit  Tab=next  Shift+Tab=prev  ?=help  Esc=back\n"
            "Dashboard: r=refresh\n"
            "Review: a=approve  r=reject  e=edit  s=skip  enter=expand\n"
            "Rules: f=filter  enter=detail  x=re-extract  d=delete\n"
            "Ingest: Select source + mode, then Start\n"
            "Settings: t=test all connections\n"
            "Graph: /=search  R=related rules  G=expand graph\n"
            "Memory: b=build  d=decay  s=start MCP",
            title="Keyboard Help",
            timeout=10,
        )

    def action_esc(self) -> None:
        for screen_id in ["screen-review", "screen-rules", "screen-ingest", "screen-graph"]:
            screen = self.query_one(f"#{screen_id}", Screen)
            for detail_id in ["review-detail", "rules-detail", "ingest-summary"]:
                try:
                    detail = screen.query_one(f"#{detail_id}", Vertical)
                    detail.styles.display = "none"
                except Exception:
                    pass
        try:
            rules_screen = self.query_one("#screen-rules", RulesScreen)
            filt = rules_screen.query_one("#rules-filter", Horizontal)
            filt.styles.display = "none"
        except Exception:
            pass
        try:
            graph_screen = self.query_one("#screen-graph", KnowledgeGraphScreen)
            inp = graph_screen.query_one("#kg-search-input", Input)
            inp.styles.display = "none"
        except Exception:
            pass
