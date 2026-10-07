"""SQLAlchemy models.

Portability: only generic types (String, Integer, Boolean, Date, DateTime,
Float, JSON, non-native Enum) so the same models run on SQLite and Postgres.
Every table carries a nullable ``tenant_id``; no tenancy behaviour exists yet.

Invariants enforced here rather than by convention:
- ``EmailObservation`` is append-only. ORM updates/deletes of observation
  rows, and bulk UPDATE/DELETE statements against the table, raise.
- ``Contact.sendable()`` is the only sanctioned path to a sendable set. It
  requires origin=confirmed AND provenance_confirmed, or origin=generated
  with a passed canary AND provenance_confirmed.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    and_,
    or_,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.sql import Delete, Update


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Origin(str, enum.Enum):
    confirmed = "confirmed"
    generated = "generated"


class CanaryStatus(str, enum.Enum):
    pending = "pending"
    passed = "passed"
    failed = "failed"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, native_enum=False, create_constraint=True, length=16,
                values_callable=lambda x: [m.value for m in x])


class EmailObservation(Base):
    """One address seen in one source row. Append-only."""

    __tablename__ = "email_observations"
    __table_args__ = (UniqueConstraint("tenant_id", "obs_key", name="uq_obs_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # sha256 of (email, source_file, row_index): the idempotency key.
    obs_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    email_raw: Mapped[str | None] = mapped_column(String(320))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    local_part: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255), index=True)

    first_name_raw: Mapped[str | None] = mapped_column(String(255))
    last_name_raw: Mapped[str | None] = mapped_column(String(255))
    stated_domain: Mapped[str | None] = mapped_column(String(255))
    company_name_raw: Mapped[str | None] = mapped_column(String(255))
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    linkedin_surname: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(128))
    extra: Mapped[dict | None] = mapped_column(JSON)

    # Provenance (first-class, not metadata).
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[date] = mapped_column(Date, nullable=False)
    observed_at_precision: Mapped[str] = mapped_column(String(16), nullable=False)
    provenance_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    delivery_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    excluded_reason: Mapped[str | None] = mapped_column(String(32), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InferenceRun(Base):
    """One execution of pattern inference. Pattern rows are immutable per run."""

    __tablename__ = "inference_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Hash of the inputs (observation keys + tunables); equal hash => equal output.
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    use_nicknames: Mapped[bool] = mapped_column(Boolean, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    patterns: Mapped[list["DomainPattern"]] = relationship(back_populates="run")


class DomainPattern(Base):
    __tablename__ = "domain_patterns"
    __table_args__ = (UniqueConstraint("run_id", "domain", name="uq_run_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("inference_runs.id"), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    tier: Mapped[str] = mapped_column(String(1), nullable=False, index=True)
    pattern: Mapped[str | None] = mapped_column(String(32))
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    weighted_support: Mapped[float] = mapped_column(Float, nullable=False)
    dominance: Mapped[float | None] = mapped_column(Float)
    patterns_seen: Mapped[dict] = mapped_column(JSON, nullable=False)
    unexplained_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    newest_sample_at: Mapped[date | None] = mapped_column(Date)
    migrated_from: Mapped[str | None] = mapped_column(String(32))
    evidence_email: Mapped[str | None] = mapped_column(String(320))
    contacts_held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(255))

    run: Mapped[InferenceRun] = relationship(back_populates="patterns")


class Contact(Base):
    """A person-address. Confirmed (observed) and generated (inferred) never share a row."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_contact_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    # Where last_name came from: linkedin | name_field | residual | target
    last_name_source: Mapped[str | None] = mapped_column(String(16))
    stated_domain: Mapped[str | None] = mapped_column(String(255))

    origin: Mapped[Origin] = mapped_column(_enum(Origin, "contact_origin"), nullable=False, index=True)
    provenance_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sources: Mapped[list | None] = mapped_column(JSON)

    # Generated-only fields.
    derived_from_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("domain_patterns.id"))
    canary_status: Mapped[CanaryStatus | None] = mapped_column(_enum(CanaryStatus, "canary_status"))
    variant_kind: Mapped[str | None] = mapped_column(String(16))  # primary | nickname | reorder

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    derived_from_pattern: Mapped[DomainPattern | None] = relationship()

    @classmethod
    def sendable(cls):
        """The only sanctioned query for a sendable set.

        Excludes: anything without confirmed provenance; generated contacts
        whose canary has not passed.
        """
        return select(cls).where(
            cls.provenance_confirmed.is_(True),
            or_(
                cls.origin == Origin.confirmed,
                and_(cls.origin == Origin.generated, cls.canary_status == CanaryStatus.passed),
            ),
        )


@event.listens_for(Contact, "before_insert")
def _generated_defaults(mapper, connection, target: Contact) -> None:
    if target.origin == Origin.generated:
        if target.canary_status is None:
            target.canary_status = CanaryStatus.pending
        if target.derived_from_pattern_id is None and target.derived_from_pattern is None:
            raise ValueError("generated contacts must carry derived_from_pattern_id")
    elif target.derived_from_pattern_id is not None or target.canary_status is not None:
        raise ValueError("confirmed contacts cannot carry generation fields")


class GenerationFlag(Base):
    """A candidate withheld from generation because of a collision."""

    __tablename__ = "generation_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # cross | self
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    other_name: Mapped[str | None] = mapped_column(String(255))
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("email_observations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- Append-only guard -----------------------------------------------------------


class AppendOnlyViolation(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _block_observation_mutation(session: Session, flush_context, instances) -> None:
    for obj in session.deleted:
        if isinstance(obj, EmailObservation):
            raise AppendOnlyViolation("email_observations is append-only: DELETE refused")
    for obj in session.dirty:
        if isinstance(obj, EmailObservation) and session.is_modified(obj, include_collections=False):
            raise AppendOnlyViolation("email_observations is append-only: UPDATE refused")


@event.listens_for(Session, "do_orm_execute")
def _block_bulk_mutation(state) -> None:
    stmt = state.statement
    if isinstance(stmt, (Update, Delete)) and stmt.table.name == EmailObservation.__tablename__:
        raise AppendOnlyViolation("email_observations is append-only: bulk UPDATE/DELETE refused")
