"""Eén denk/werk-cyclus van de gezins-agent via de Claude Agent SDK."""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .config import Config
from .ledger import Ledger

log = logging.getLogger("fien")

ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch", "WebFetch"]

BUDGET_MSG = (
    "⏸️ Ik pauzeer even: het dagbudget is bereikt. Morgen sta ik er weer. "
    "Je taken zijn veilig opgeslagen."
)


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, timeout=120)


def ensure_git(workspace: Path) -> None:
    if not (workspace / ".git").exists():
        _git(workspace, "init")
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-m", "Start gezinswerkruimte", "--allow-empty")


def commit(workspace: Path, label: str) -> None:
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", label, "--allow-empty")


class Brain:
    def __init__(self, cfg: Config, ledger: Ledger):
        self.cfg = cfg
        self.ledger = ledger

    def _prompt(self, name: str, **fmt) -> str:
        return (self.cfg.prompts_dir / name).read_text().format(**fmt)

    async def run(self, prompt_name: str, label: str, **fmt) -> str | None:
        """Draai één cyclus. Geeft de antwoordtekst terug (of None bij stilte/fout)."""
        if not self.ledger.within_budget(self.cfg.daily_budget_usd):
            log.warning("dagbudget bereikt ($%.2f)", self.ledger.spent_today())
            return BUDGET_MSG

        fmt.setdefault("now", datetime.now().strftime("%A %d-%m-%Y %H:%M"))
        user_prompt = self._prompt(prompt_name, **fmt)
        system_prompt = (self.cfg.prompts_dir / "system.md").read_text().replace(
            "{agent_name}", self.cfg.agent_name
        )

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=str(self.cfg.workspace),
            allowed_tools=ALLOWED_TOOLS,
            disallowed_tools=["Task"],
            permission_mode="acceptEdits",
            max_turns=self.cfg.max_turns,
            model=self.cfg.model,
            # Foto's/pdf's komen als base64 door de SDK-stream; de standaardbuffer
            # van 1 MB is te klein voor een telefoonfoto.
            max_buffer_size=32 * 1024 * 1024,
        )

        reply: str | None = None
        cost = 0.0
        try:
            async for message in query(prompt=user_prompt, options=options):
                if isinstance(message, ResultMessage):
                    cost = max(cost, float(getattr(message, "total_cost_usd", 0.0) or 0.0))
                    text = getattr(message, "result", None)
                    if isinstance(text, str) and text.strip():
                        reply = text.strip()
                    if cost >= self.cfg.cycle_budget_usd:
                        log.warning("cyclusbudget bereikt ($%.2f) — stop", cost)
                        break
        except Exception:
            log.exception("cyclus '%s' mislukt", label)
            reply = None
        finally:
            self.ledger.record(cost)
            commit(self.cfg.workspace, f"{label} — {datetime.now():%d-%m %H:%M} (${cost:.2f})")
            log.info("cyclus '%s' klaar: $%.4f", label, cost)
        return reply
