"""Book configuration containment and reproducible experiment storage boundaries."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
import yaml
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.dml import Update
from typer.testing import CliRunner

from trader.agent.catalog import PipelineCatalog
from trader.books.experiments import (
    begin_book_evaluation,
    fail_book_evaluation,
    finish_book_evaluation,
    mark_interrupted_book_evaluation,
)
from trader.books.service import (
    open_book,
    read_operating_note,
    read_strategy_document,
    set_book_status,
    sync_strategy_document,
)
from trader.cli import app
from trader.persistence.db import create_session_factory
from trader.persistence.models import (
    AgentInvocation,
    Base,
    Book,
    BookEvaluation,
    BookExperimentPhase,
    Run,
    SimulatedFill,
    TradeProposalRecord,
)
from trader.persistence.repositories import PersistenceConflictError

ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 9, 5, 12, tzinfo=UTC)
PREVIOUS = "c9a3d7e21f48"
REVISION = "b83e219fc641"


@pytest.fixture
def catalog():
    profile = {
        "description": "One decision",
        "steps": [{"role": "daily_trader", "step": "decide", "output": "daily_decision"}],
    }
    return PipelineCatalog.model_validate(
        {
            "version": 1,
            "default_profile": "single_pass",
            "profiles": {"single_pass": profile, "trial": profile},
        }
    )


@pytest.fixture
def project(tmp_path, catalog):
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "knowledge").mkdir()
    (root / "knowledge/strategy.md").write_text("Investigate before trading.\n")
    (root / "knowledge/note.md").write_text("Challenge unsupported claims.\n")
    risk = yaml.safe_load((ROOT / "config/risk.yaml").read_text())
    risk["portfolio"]["expected_max_equity_usd"] = 1500
    (root / "config/risk.yaml").write_text(yaml.safe_dump(risk))
    agents = yaml.safe_load((ROOT / "config/agents.yaml").read_text())
    for role in agents["roles"].values():
        prompt = root / role["prompt"]
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("Role instructions.\n")
    (root / "config/agents.yaml").write_text(yaml.safe_dump(agents))
    (root / "config/pipelines.yaml").write_text(yaml.safe_dump(catalog.model_dump(mode="json")))
    return root


@pytest.fixture
def session():
    with create_session_factory("sqlite:///:memory:")() as session:
        yield session


def _open(session, project, catalog, **kwargs):
    return open_book(
        session,
        name=kwargs.pop("name", "trial-book"),
        starting_cash=kwargs.pop("starting_cash", Decimal("1000")),
        strategy_document_path=kwargs.pop("strategy_document_path", Path("knowledge/strategy.md")),
        project_root=project,
        catalog=catalog,
        as_of=MOMENT,
        **kwargs,
    )


def _run(session, key):
    run = Run(run_key=key, scheduled_for=MOMENT, config_hash="config")
    session.add(run)
    session.commit()
    return run


def _begin(session, book, key, configuration=None, **kwargs):
    run = _run(session, key)
    base_configuration = {
        "strategy_hash": "a",
        "profile": {
            "steps": [
                {"role": "daily_trader", "step": "decide", "output": "daily_decision"}
            ]
        },
    }
    if configuration is not None:
        base_configuration.update(configuration)
    return begin_book_evaluation(
        session,
        book=book,
        run_id=run.id,
        as_of=kwargs.pop("as_of", MOMENT),
        configuration=base_configuration,
        inputs=kwargs.pop("inputs", {"evidence_hash": key}),
        **kwargs,
    )


def _invocation(session, evaluation, **kwargs):
    book = session.get(Book, evaluation.book_id)
    prefix = f"book_{book.id.replace('-', '')}_{book.name.replace('-', '_')}_"
    invocation = AgentInvocation(
        run_id=kwargs.pop("run_id", evaluation.run_id),
        role=kwargs.pop("role", "daily_trader"),
        step=kwargs.pop("step", prefix + "decide"),
        purpose="book decision",
        model="test",
        provider="test",
        prompt_version="hash",
        request_path="request.json",
        status=kwargs.pop("status", "COMPLETED"),
    )
    session.add(invocation)
    session.commit()
    return invocation


def _finish(session, evaluation):
    invocation = _invocation(session, evaluation)
    return finish_book_evaluation(session, evaluation, terminal_invocation_id=invocation.id)


def test_open_persists_resolved_paths_profile_and_legacy_defaults(session, project, catalog):
    book = _open(
        session,
        project,
        catalog,
        process_profile="trial",
        operating_note_path=Path("knowledge/note.md"),
    )
    assert book.process_profile == "trial"
    assert book.strategy_document_path == str(project / "knowledge/strategy.md")
    assert book.operating_note_path == str(project / "knowledge/note.md")
    default = _open(session, project, catalog, name="default-book")
    assert default.process_profile == "single_pass"
    assert default.operating_note_path is None


def test_default_catalog_and_human_cash_ceiling_are_loaded(session, project):
    book = _open(session, project, None, starting_cash=Decimal("1500"))
    assert book.starting_cash == "1500"
    with pytest.raises(ValueError, match="ceiling"):
        _open(session, project, None, name="too-large", starting_cash=Decimal("1500.01"))


@pytest.mark.parametrize("cash", ["0", "-1", "NaN", "Infinity", "1500.01"])
def test_invalid_cash_does_not_open_book(session, project, catalog, cash):
    with pytest.raises(ValueError):
        _open(session, project, catalog, starting_cash=Decimal(cash))
    assert session.scalar(select(func.count()).select_from(Book)) == 0


@pytest.mark.parametrize("ceiling", ["0", "-1", "NaN", "Infinity"])
def test_invalid_injected_ceiling_fails_closed(session, project, catalog, ceiling):
    with pytest.raises(ValueError, match="maximum book"):
        _open(session, project, catalog, max_starting_cash=Decimal(ceiling))


def test_explicit_ceiling_does_not_require_risk_file(session, project, catalog):
    (project / "config/risk.yaml").unlink()
    book = _open(session, project, catalog, max_starting_cash=Decimal("1000"))
    assert book.starting_cash == "1000"


def test_unknown_profile_and_missing_policy_fail_closed(session, project, catalog):
    with pytest.raises(ValueError, match="unknown process profile"):
        _open(session, project, catalog, process_profile="invented")
    (project / "config/risk.yaml").unlink()
    with pytest.raises(ValueError):
        _open(session, project, catalog)
    assert session.scalar(select(func.count()).select_from(Book)) == 0


@pytest.mark.parametrize("status", ["active", "paused", "retired"])
def test_reserved_names_include_all_book_statuses(session, project, catalog, status):
    book = _open(session, project, catalog)
    set_book_status(session, book, status)
    with pytest.raises(PersistenceConflictError, match="already exists"):
        _open(session, project, catalog, name="TRIAL_book")


@pytest.mark.parametrize("field", ["strategy_document_path", "operating_note_path"])
@pytest.mark.parametrize("symlink", [False, True])
def test_escaping_paths_and_symlinks_are_rejected(session, project, catalog, field, symlink):
    outside = project.parent / "outside.md"
    outside.write_text("External instructions")
    supplied = Path("../outside.md")
    if symlink:
        supplied = Path("knowledge/link.md")
        (project / supplied).symlink_to(outside)
    with pytest.raises(ValueError, match="contained project file"):
        _open(session, project, catalog, **{field: supplied})
    assert session.scalar(select(func.count()).select_from(Book)) == 0


def test_contained_symlink_and_cwd_resolution(project, monkeypatch):
    (project / "knowledge/link.md").symlink_to(project / "knowledge/strategy.md")
    monkeypatch.chdir(project)
    assert read_strategy_document(Path("knowledge/link.md")) == "Investigate before trading."
    assert read_operating_note(None, project_root=project) == ""


@pytest.mark.parametrize("content", ["", " \n", "x" * 8001, "x\x00", "x\x7f", "x\u202e"])
def test_invalid_operating_notes_are_rejected(project, content):
    note = project / "knowledge/note.md"
    note.write_text(content)
    with pytest.raises(ValueError):
        read_operating_note(note, project_root=project)


def test_operating_note_boundary_and_allowed_whitespace(project):
    note = project / "knowledge/note.md"
    note.write_text("x" * 8000)
    assert len(read_operating_note(note, project_root=project)) == 8000
    note.write_text("one\n\ttwo\n")
    assert read_operating_note(note, project_root=project) == "one\n\ttwo"


@pytest.mark.parametrize("field", ["strategy_document_path", "operating_note_path"])
def test_sync_rejects_legacy_escape_without_mutating_book(session, project, catalog, field):
    book = _open(session, project, catalog)
    original_hash = book.strategy_content_hash
    outside = project.parent / "legacy.md"
    outside.write_text("Legacy external document")
    setattr(book, field, str(outside))
    session.commit()
    with pytest.raises(ValueError, match="contained project file"):
        sync_strategy_document(session, book, as_of=MOMENT, project_root=project)
    assert book.strategy_content_hash == original_hash
    assert getattr(book, field) == str(outside)


def test_sync_updates_contained_strategy(session, project, catalog):
    book = _open(session, project, catalog)
    (project / "knowledge/strategy.md").write_text("New hypothesis")
    document, changed = sync_strategy_document(session, book, as_of=MOMENT, project_root=project)
    assert changed and document == "New hypothesis"
    assert not sync_strategy_document(session, book, as_of=MOMENT, project_root=project)[1]


def test_phases_reuse_last_only_and_record_distinct_run_provenance(session, project, catalog):
    book = _open(session, project, catalog)
    a = {"strategy_hash": "a", "model": {"effort": "high", "name": "test"}}
    first = _begin(session, book, "first", a)
    _finish(session, first)
    same = _begin(session, book, "same", {"model": a["model"], "strategy_hash": "a"})
    _finish(session, same)
    second = _begin(session, book, "second", {"strategy_hash": "b"})
    _finish(session, second)
    third = _begin(session, book, "third", a)
    assert first.phase_id == same.phase_id
    assert len({first.phase_id, second.phase_id, third.phase_id}) == 3
    phases = session.scalars(
        select(BookExperimentPhase).order_by(BookExperimentPhase.ordinal)
    ).all()
    assert [phase.ordinal for phase in phases] == [1, 2, 3]
    assert phases[0].configuration_hash == phases[2].configuration_hash
    for phase in phases:
        assert phase.configuration_hash == hashlib.sha256(phase.manifest_json.encode()).hexdigest()
        assert json.loads(phase.manifest_json)["version"] == 1
    assert json.loads(first.manifest_json) == {"version": 1, "inputs": {"evidence_hash": "first"}}
    assert first.manifest_json != same.manifest_json
    a["strategy_hash"] = "mutated caller object"
    assert json.loads(phases[0].manifest_json)["configuration"]["strategy_hash"] == "a"


def test_duplicate_evaluation_checks_before_new_phase(session, project, catalog):
    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "original")
    with pytest.raises(PersistenceConflictError, match="already evaluated"):
        begin_book_evaluation(
            session,
            book=book,
            run_id=evaluation.run_id,
            as_of=MOMENT,
            configuration={"strategy_hash": "changed"},
            inputs={},
        )
    assert session.scalar(select(func.count()).select_from(BookExperimentPhase)) == 1
    assert session.scalar(select(func.count()).select_from(BookEvaluation)) == 1
    assert not session.new


@pytest.mark.parametrize("configuration", [{"strategy_hash": "a"}, {"strategy_hash": "changed"}])
def test_backdated_evaluation_does_not_fabricate_phase(session, project, catalog, configuration):
    book = _open(session, project, catalog)
    _finish(session, _begin(session, book, "today"))
    with pytest.raises(PersistenceConflictError, match="backdated"):
        _begin(session, book, "yesterday", configuration, as_of=MOMENT - timedelta(days=1))
    assert session.scalar(select(func.count()).select_from(BookExperimentPhase)) == 1
    assert session.scalar(select(func.count()).select_from(BookEvaluation)) == 1


@pytest.mark.parametrize(
    "configuration", [{"nan": float("nan")}, {1: "bad"}, {"money": Decimal(1)}]
)
def test_invalid_manifest_does_not_create_phase(session, project, catalog, configuration):
    book = _open(session, project, catalog)
    with pytest.raises(ValueError):
        _begin(session, book, "invalid", configuration)
    assert session.scalar(select(func.count()).select_from(BookExperimentPhase)) == 0


def test_naive_time_rejected_and_sqlite_reloaded_times_work(session, project, catalog):
    book = _open(session, project, catalog)
    with pytest.raises(ValueError, match="timezone-aware"):
        _begin(session, book, "naive", as_of=MOMENT.replace(tzinfo=None))
    first = _begin(session, book, "first")
    _finish(session, first)
    session.expire_all()
    later = _begin(session, book, "later", as_of=MOMENT + timedelta(days=1))
    assert first.phase_id == later.phase_id


def test_corrupted_phase_fails_closed(session, project, catalog):
    book = _open(session, project, catalog)
    first = _begin(session, book, "first")
    _finish(session, first)
    phase = session.get(BookExperimentPhase, first.phase_id)
    phase.manifest_json = "{}"
    session.commit()
    with pytest.raises(PersistenceConflictError, match="hash mismatch"):
        _begin(session, book, "next")


def test_complete_and_failed_outcomes_cannot_be_overwritten(session, project, catalog):
    book = _open(session, project, catalog)
    completed = _begin(session, book, "complete")
    invocation = _invocation(session, completed)
    finish_book_evaluation(session, completed, terminal_invocation_id=invocation.id)
    assert completed.status == "COMPLETED" and completed.terminal_invocation_id == invocation.id
    with pytest.raises(PersistenceConflictError, match="already resolved"):
        fail_book_evaluation(session, completed, error="late failure")
    failed = _begin(session, book, "failure")
    manifest = failed.manifest_json
    fail_book_evaluation(session, failed, error="Invalid evidence ID")
    assert failed.status == "FAILED" and failed.error == "Invalid evidence ID"
    assert failed.phase_id == completed.phase_id and failed.manifest_json == manifest
    own_invocation = _invocation(session, failed)
    with pytest.raises(PersistenceConflictError, match="already resolved"):
        finish_book_evaluation(session, failed, terminal_invocation_id=own_invocation.id)


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "wrong_run",
        "wrong_role",
        "wrong_step",
        "unfinished",
        "live",
        "sibling",
        "no_step",
    ],
)
def test_completion_requires_valid_terminal_invocation(session, project, catalog, invalid):
    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "first")
    kwargs = {}
    if invalid == "wrong_run":
        kwargs["run_id"] = _run(session, "other").id
    elif invalid == "wrong_role":
        kwargs["role"] = "research_compactor"
    elif invalid == "wrong_step":
        kwargs["step"] = (
            f"book_{book.id.replace('-', '')}_{book.name.replace('-', '_')}_packet"
        )
    elif invalid == "unfinished":
        kwargs["status"] = "STARTED"
    elif invalid == "live":
        kwargs["step"] = "decide"
    elif invalid == "sibling":
        sibling = _open(session, project, catalog, name="sibling-book")
        kwargs["step"] = f"book_{sibling.id.replace('-', '')}_sibling_book_decide"
    elif invalid == "no_step":
        kwargs["step"] = f"book_{book.id.replace('-', '')}_{book.name.replace('-', '_')}_"
    invocation_id = (
        "missing" if invalid == "missing" else _invocation(session, evaluation, **kwargs).id
    )
    with pytest.raises(ValueError, match="terminal invocation"):
        finish_book_evaluation(session, evaluation, terminal_invocation_id=invocation_id)
    assert evaluation.status == "STARTED"
    with pytest.raises(ValueError, match="nonempty error"):
        fail_book_evaluation(session, evaluation, error=" ")


def test_database_enforces_evaluation_uniqueness_and_outcome(session, project, catalog):
    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "first")
    with pytest.raises(IntegrityError):
        session.execute(text("UPDATE book_evaluations SET status = 'UNKNOWN'"))
    session.rollback()
    with pytest.raises(IntegrityError):
        session.execute(text("UPDATE book_evaluations SET status = 'COMPLETED'"))
    session.rollback()
    duplicate = BookEvaluation(
        book_id=book.id,
        run_id=evaluation.run_id,
        phase_id=evaluation.phase_id,
        as_of=MOMENT,
        manifest_json="{}",
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_overlapping_run_refused_before_phase_mutation(session, project, catalog):
    book = _open(session, project, catalog)
    first = _begin(session, book, "first")
    phase = session.get(BookExperimentPhase, first.phase_id)
    original_manifest = phase.manifest_json
    with pytest.raises(PersistenceConflictError, match="active evaluation"):
        _begin(session, book, "overlap", {"strategy_hash": "different"})
    assert session.scalar(select(func.count()).select_from(BookExperimentPhase)) == 1
    assert session.scalar(select(func.count()).select_from(BookEvaluation)) == 1
    assert phase.manifest_json == original_manifest
    assert first.status == "STARTED"
    assert not session.new and not session.dirty
    assert session.scalar(select(func.count()).select_from(TradeProposalRecord)) == 0
    assert session.scalar(select(func.count()).select_from(SimulatedFill)) == 0


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
def test_resolved_claim_releases_book_but_never_same_run(session, project, catalog, status):
    book = _open(session, project, catalog)
    first = _begin(session, book, "first")
    if status == "COMPLETED":
        _finish(session, first)
    else:
        fail_book_evaluation(session, first, error="Invalid packet")
    with pytest.raises(PersistenceConflictError, match="already evaluated"):
        begin_book_evaluation(
            session, book=book, run_id=first.run_id, as_of=MOMENT, configuration={}, inputs={}
        )
    second = _begin(session, book, "second")
    assert second.status == "STARTED"
    assert second.phase_id == first.phase_id


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_active_book_index_is_partial_on_supported_databases(dialect):
    index = next(
        item
        for item in BookEvaluation.__table__.indexes
        if item.name == "uq_book_evaluations_active_book"
    )
    ddl = str(CreateIndex(index).compile(dialect=dialect))
    assert "CREATE UNIQUE INDEX" in ddl
    assert "(book_id) WHERE status = 'STARTED'" in ddl


@pytest.mark.parametrize("parent_status", ["STARTED", "COMPLETED"])
def test_interruption_recovery_refuses_nonfailed_parent(session, project, catalog, parent_status):
    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "first")
    parent = session.get(Run, evaluation.run_id)
    parent.status = parent_status
    session.commit()
    with pytest.raises(ValueError, match="FAILED parent run"):
        mark_interrupted_book_evaluation(
            session, evaluation_id=evaluation.id, reviewer="operator", note="Inspect interruption"
        )
    assert evaluation.status == "STARTED" and evaluation.error is None
    with pytest.raises(PersistenceConflictError, match="active evaluation"):
        _begin(session, book, "still-locked")


@pytest.mark.parametrize(
    ("role", "namespace", "other_run"),
    [
        ("daily_trader", "current", False),
        ("research_compactor", "current", False),
        ("research_compactor", "current", True),
        ("daily_trader", "legacy", False),
        ("daily_trader", "renamed", True),
    ],
)
def test_interruption_recovery_refuses_book_invocations(
    session, project, catalog, role, namespace, other_run
):
    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "interrupted")
    parent = session.get(Run, evaluation.run_id)
    parent.status = "FAILED"
    session.commit()
    kwargs = {"role": role, "status": "STARTED"}
    if namespace == "legacy":
        kwargs["step"] = f"book_{book.name.replace('-', '_')}"
    elif namespace == "renamed":
        kwargs["step"] = f"book_{book.id.replace('-', '')}_old_name_packet"
    if other_run:
        kwargs["run_id"] = _run(session, "other").id
    invocation = _invocation(session, evaluation, **kwargs)
    with pytest.raises(ValueError, match="STARTED agent invocations"):
        mark_interrupted_book_evaluation(
            session, evaluation_id=evaluation.id, reviewer="operator", note="Process stopped"
        )
    assert invocation.status == evaluation.status == "STARTED"
    assert evaluation.error is None


def test_interruption_recovery_retains_audit_and_settlement(session, project, catalog):
    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "interrupted")
    original = (evaluation.phase_id, evaluation.manifest_json, evaluation.run_id)
    parent = session.get(Run, evaluation.run_id)
    parent.status = "FAILED"
    session.commit()
    invocation = _invocation(session, evaluation)
    # An unrelated active book invocation must not keep this book locked.
    sibling = _open(session, project, catalog, name="sibling-book")
    other = _invocation(
        session,
        evaluation,
        status="STARTED",
        step=f"book_{sibling.id.replace('-', '')}_sibling_book_packet",
        role="research_compactor",
    )
    fill = SimulatedFill(
        book_id=book.id,
        run_id=evaluation.run_id,
        proposal_id="retained-proposal",
        symbol="SPY",
        side="buy",
        qty="1",
        price="100",
        commission="0",
        assumptions_json="{}",
        transaction_time=MOMENT,
    )
    session.add(fill)
    session.commit()
    recovered = mark_interrupted_book_evaluation(
        session,
        evaluation_id=evaluation.id,
        reviewer="  Jack  ",
        note="Verified the process stopped; retain the existing fill.",
    )
    assert recovered.status == "FAILED" and recovered.terminal_invocation_id is None
    assert (recovered.phase_id, recovered.manifest_json, recovered.run_id) == original
    reason = json.loads(recovered.error)
    assert reason["type"] == "OPERATOR_MARKED_INTERRUPTED"
    assert reason["reviewer"] == "Jack"
    assert reason["note"] == "Verified the process stopped; retain the existing fill."
    assert datetime.fromisoformat(reason["recorded_at"]).tzinfo is not None
    assert parent.status == "FAILED"
    assert invocation.status == "COMPLETED" and other.status == "STARTED"
    assert session.get(SimulatedFill, fill.id).price == "100"
    with pytest.raises(PersistenceConflictError, match="already resolved"):
        mark_interrupted_book_evaluation(
            session, evaluation_id=evaluation.id, reviewer="another", note="Overwrite"
        )
    with pytest.raises(PersistenceConflictError, match="already evaluated"):
        begin_book_evaluation(
            session,
            book=book,
            run_id=evaluation.run_id,
            as_of=MOMENT,
            configuration={},
            inputs={},
        )
    assert _begin(session, book, "new-run").phase_id == evaluation.phase_id


def test_cli_recovers_only_a_verified_interrupted_evaluation(
    session, project, catalog, monkeypatch
):
    import trader.cli as cli

    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "interrupted")
    parent = session.get(Run, evaluation.run_id)
    parent.status = "FAILED"
    session.commit()
    monkeypatch.setattr(cli, "database_service", lambda: (object(), session))

    result = CliRunner().invoke(
        app,
        [
            "books",
            "recover-evaluation",
            evaluation.id,
            "--reviewer",
            "Jack",
            "--note",
            "Verified the interrupted process is no longer running.",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "FAILED"
    assert payload["error"]["type"] == "OPERATOR_MARKED_INTERRUPTED"


@pytest.mark.parametrize(
    ("reviewer", "note"),
    [(" ", "note"), ("x" * 201, "note"), ("reviewer", " "), ("reviewer", "x" * 4001)],
)
def test_interruption_recovery_requires_bounded_operator_record(session, reviewer, note):
    with pytest.raises(ValueError, match="interruption"):
        mark_interrupted_book_evaluation(
            session, evaluation_id="missing", reviewer=reviewer, note=note
        )


def test_interruption_recovery_requires_existing_evaluation(session):
    with pytest.raises(LookupError, match="not found"):
        mark_interrupted_book_evaluation(
            session, evaluation_id="missing", reviewer="operator", note="Inspected"
        )


def test_recovery_checks_persisted_parent_not_stale_session(session, project, catalog):
    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "interrupted")
    parent = session.get(Run, evaluation.run_id)
    parent.status = "FAILED"
    session.commit()
    with Session(session.get_bind()) as other:
        other.execute(text("UPDATE runs SET status = 'STARTED' WHERE id = :id"), {"id": parent.id})
        other.commit()
    assert parent.status == "FAILED"  # Cached object must not authorize recovery.
    with pytest.raises(ValueError, match="FAILED parent run"):
        mark_interrupted_book_evaluation(
            session, evaluation_id=evaluation.id, reviewer="operator", note="Inspected"
        )
    assert evaluation.status == "STARTED"


@pytest.mark.parametrize("change", ["parent", "invocation"])
def test_recovery_rechecks_conditions_atomically(session, project, catalog, monkeypatch, change):
    book = _open(session, project, catalog)
    evaluation = _begin(session, book, "interrupted")
    parent = session.get(Run, evaluation.run_id)
    parent.status = "FAILED"
    session.commit()
    scalar = session.scalar

    def changed_before_update(statement, *args, **kwargs):
        if isinstance(statement, Update):
            with Session(session.get_bind()) as other:
                if change == "parent":
                    other.execute(
                        text("UPDATE runs SET status = 'STARTED' WHERE id = :id"), {"id": parent.id}
                    )
                    other.commit()
                else:
                    _invocation(other, evaluation, status="STARTED")
        return scalar(statement, *args, **kwargs)

    monkeypatch.setattr(session, "scalar", changed_before_update)
    with pytest.raises(PersistenceConflictError, match="preconditions changed"):
        mark_interrupted_book_evaluation(
            session, evaluation_id=evaluation.id, reviewer="operator", note="Inspected"
        )
    assert evaluation.status == "STARTED" and evaluation.error is None


def _migration(tmp_path):
    url = f"sqlite:///{tmp_path}/migration.sqlite"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, PREVIOUS)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO books (id, name, strategy_document_path, strategy_content_hash, "
                "status, starting_cash, created_at, updated_at) VALUES "
                "('legacy', 'legacy-book', 'knowledge/strategy.md', 'hash', "
                "'active', '1000', :t, :t)"
            ),
            {"t": MOMENT},
        )
    return config, engine, url


def test_migration_preserves_legacy_books_and_default_downgrade(tmp_path):
    config, engine, _ = _migration(tmp_path)
    command.upgrade(config, REVISION)
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert tuple(
            connection.execute(
                text("SELECT process_profile, operating_note_path FROM books WHERE id = 'legacy'")
            ).one()
        ) == ("single_pass", None)
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert differences == []
    command.downgrade(config, PREVIOUS)
    assert "process_profile" not in {
        column["name"] for column in inspect(engine).get_columns("books")
    }
    assert "book_evaluations" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM books")).scalar_one() == "legacy-book"


def test_usage_migration_backfills_namespaced_book_invocations(tmp_path):
    config, engine, _ = _migration(tmp_path)
    command.upgrade(config, "a71d5e30c924")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs (id, run_key, mode, scheduled_for, started_at, status, "
                "config_hash) VALUES ('usage-run', 'daily:usage', 'paper', :t, :t, "
                "'STARTED', 'config')"
            ),
            {"t": MOMENT},
        )
        connection.execute(
            text(
                "INSERT INTO book_experiment_phases "
                "(id, book_id, ordinal, configuration_hash, manifest_json, created_at) VALUES "
                "('usage-phase', 'legacy', 1, 'hash', '{}', :t)"
            ),
            {"t": MOMENT},
        )
        connection.execute(
            text(
                "INSERT INTO book_evaluations "
                "(id, book_id, run_id, phase_id, as_of, status, manifest_json) VALUES "
                "('usage-evaluation', 'legacy', 'usage-run', 'usage-phase', :t, "
                "'STARTED', '{}')"
            ),
            {"t": MOMENT},
        )
        connection.execute(
            text(
                "INSERT INTO agent_invocations "
                "(id, run_id, role, step, attempt, purpose, model, provider, prompt_version, "
                "request_path, input_token_count, output_token_count, started_at, status) VALUES "
                "('usage-invocation', 'usage-run', 'research_compactor', "
                "'book_legacy_legacy_book_packet', 1, 'test', 'model', 'provider', 'prompt', "
                "'request.json', 100, 25, :t, 'COMPLETED')"
            ),
            {"t": MOMENT},
        )

    command.upgrade(config, REVISION)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT book_id, book_evaluation_id, cost_source, total_token_count "
                "FROM agent_invocations "
                "WHERE id = 'usage-invocation'"
            )
        ).one()
    assert tuple(row) == ("legacy", "usage-evaluation", "NOT_REPORTED", 125)


@pytest.mark.parametrize("through_service", [True, False])
def test_two_sessions_contend_for_one_book_before_any_money_changes(tmp_path, through_service):
    config, engine, url = _migration(tmp_path)
    command.upgrade(config, REVISION)
    factory = create_session_factory(url, create_schema=False)
    with factory() as setup:
        book = setup.get(Book, "legacy")
        previous = _finish(setup, _begin(setup, book, "previous"))
        phase_id = previous.phase_id
        phase_configuration = json.loads(
            setup.get(BookExperimentPhase, phase_id).manifest_json
        )["configuration"]
        run_ids = [_run(setup, name).id for name in ("daily:first", "daily-test:second")]
    barrier = Barrier(2)

    def contend(run_id):
        with factory() as worker:
            book = worker.get(Book, "legacy")
            barrier.wait(timeout=10)
            try:
                if through_service:
                    claimed = begin_book_evaluation(
                        worker,
                        book=book,
                        run_id=run_id,
                        as_of=MOMENT,
                        configuration=phase_configuration,
                        inputs={"run": run_id},
                    )
                else:
                    # Both sessions bypass the service's precheck, exercising the actual index.
                    claimed = BookEvaluation(
                        book_id=book.id,
                        run_id=run_id,
                        phase_id=phase_id,
                        as_of=MOMENT,
                        manifest_json="{}",
                    )
                    worker.add(claimed)
                    worker.commit()
                return "claimed", claimed.id
            except (IntegrityError, PersistenceConflictError):
                worker.rollback()
                return "refused", None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(contend, run_ids))
    assert sorted(result for result, _ in outcomes) == ["claimed", "refused"]
    with factory() as check:
        started = check.scalars(
            select(BookEvaluation).where(BookEvaluation.status == "STARTED")
        ).all()
        assert len(started) == 1
        assert check.scalar(select(func.count()).select_from(BookEvaluation)) == 2
        assert check.scalar(select(func.count()).select_from(BookExperimentPhase)) == 1
        assert check.scalar(select(func.count()).select_from(TradeProposalRecord)) == 0
        assert check.scalar(select(func.count()).select_from(SimulatedFill)) == 0
        assert check.get(Book, "legacy").starting_cash == "1000"
        # Resolved history cannot be reactivated while a new owner holds the book.
        with pytest.raises(IntegrityError):
            check.execute(
                text(
                    "UPDATE book_evaluations SET status = 'STARTED', terminal_invocation_id = NULL "
                    "WHERE id = :id"
                ),
                {"id": previous.id},
            )
        check.rollback()
    engine.dispose()


@pytest.mark.parametrize("kind", ["profile", "note", "phase", "evaluation"])
def test_migration_guards_customization_and_history(tmp_path, kind):
    config, engine, url = _migration(tmp_path)
    command.upgrade(config, REVISION)
    with create_session_factory(url, create_schema=False)() as session:
        book = session.get(Book, "legacy")
        if kind == "profile":
            book.process_profile = "trial"
        elif kind == "note":
            book.operating_note_path = "knowledge/note.md"
        elif kind == "phase":
            session.add(
                BookExperimentPhase(
                    book_id=book.id,
                    ordinal=1,
                    configuration_hash="hash",
                    manifest_json="{}",
                )
            )
        else:
            _begin(session, book, "history")
        session.commit()
    with pytest.raises(RuntimeError, match="experiment history"):
        command.downgrade(config, PREVIOUS)
    assert "book_evaluations" in inspect(engine).get_table_names()
    assert "process_profile" in {column["name"] for column in inspect(engine).get_columns("books")}
