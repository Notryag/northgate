from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_migration_history_has_one_linear_head() -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()
    assert len(heads) == 1

    revisions = list(script.walk_revisions(base="base", head="heads"))
    assert revisions[-1].down_revision is None
    for revision in revisions:
        assert callable(revision.module.upgrade)
        assert callable(revision.module.downgrade)
    for current, parent in zip(revisions, revisions[1:], strict=False):
        assert current.down_revision == parent.revision
