"""归档目录不得依赖 CWD（P1-2）：须为绝对路径且位于仓库根之下。"""

from aistock_agent.agents.workers import rhythm_master
from aistock_agent.services import rhythm_verification
from aistock_agent.utils.paths import project_root


def test_archive_dirs_are_absolute_and_under_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = project_root()
    assert root.is_absolute()

    assert rhythm_master.sentiment_archive_dir.is_absolute()
    assert root in rhythm_master.sentiment_archive_dir.parents

    assert rhythm_verification.verification_dir.is_absolute()
    assert root in rhythm_verification.verification_dir.parents
