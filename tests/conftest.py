import shutil
import subprocess
from pathlib import Path

import pytest

from bc_mcp.config import Config, GitConfig, LedgerConfig
from bc_mcp.gitops import GitRepo
from bc_mcp.ledger import Ledger
from bc_mcp.writer import Writer

# A small made-up ledger in the same layout as a real one (books/YYYY/MM/MM.beancount).
SAMPLE_LEDGER = Path(__file__).resolve().parent / "fixtures" / "ledger"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    """A git-tracked copy of the mock ledger; tests never touch the fixture itself."""
    root = tmp_path / "ledger"
    shutil.copytree(SAMPLE_LEDGER, root)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")
    return root


@pytest.fixture
def config(ledger_dir: Path) -> Config:
    return Config(ledger=LedgerConfig(root=ledger_dir))


@pytest.fixture
def ledger(config: Config) -> Ledger:
    return Ledger(config.ledger)


@pytest.fixture
def writer(ledger: Ledger, config: Config) -> Writer:
    return Writer(ledger, GitRepo(ledger.root, GitConfig()))
