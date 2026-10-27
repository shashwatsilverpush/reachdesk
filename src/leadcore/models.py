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


class UserRole(str, enum.Enum):
    admin = "admin"
    manager = "manager"
    member = "member"


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
    subtier: Mapped[str | None] = mapped_column(String(4))

    @property
    def grade(self) -> str:
        """Tier with the C+/C- split applied."""
        return self.subtier or self.tier
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


class LeadStatus(str, enum.Enum):
    new = "new"
    contacted = "contacted"
    following_up = "following_up"
    replied = "replied"
    meeting = "meeting"
    won = "won"
    lost = "lost"


class CRMUser(Base):
    """A team member who performs outreach."""

    __tablename__ = "crm_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    role: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manager_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("crm_users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    manager: Mapped["CRMUser | None"] = relationship("CRMUser", remote_side=[id], foreign_keys=[manager_id])
    leads: Mapped[list["Lead"]] = relationship(back_populates="assigned_user")


class Lead(Base):
    """A prospect assigned to a user for outreach."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("crm_users.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))

    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    company: Mapped[str | None] = mapped_column(String(255))
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    role_title: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    country: Mapped[str | None] = mapped_column(String(100))

    status: Mapped[LeadStatus] = mapped_column(
        _enum(LeadStatus, "lead_status"), nullable=False, default=LeadStatus.new, index=True,
    )
    notes: Mapped[str | None] = mapped_column(String(2000))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    assigned_user: Mapped[CRMUser | None] = relationship(back_populates="leads")
    contact: Mapped[Contact | None] = relationship()
    lead_sequence: Mapped["LeadSequence | None"] = relationship(back_populates="lead", uselist=False)
    activities: Mapped[list["LeadActivity"]] = relationship(back_populates="lead", order_by="LeadActivity.created_at.desc()")


class MailTemplate(Base):
    """A reusable email reachout format."""

    __tablename__ = "mail_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("crm_users.id"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(String(10000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    owner: Mapped[CRMUser | None] = relationship()


class Sequence(Base):
    """A reusable multi-step email sequence."""

    __tablename__ = "sequences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("crm_users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    steps: Mapped[list["SequenceStep"]] = relationship(back_populates="sequence", order_by="SequenceStep.step_number")
    owner: Mapped[CRMUser] = relationship()


class SequenceStep(Base):
    """One step in a sequence."""

    __tablename__ = "sequence_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sequence_id: Mapped[int] = mapped_column(ForeignKey("sequences.id"), nullable=False)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subject_template: Mapped[str] = mapped_column(String(500), nullable=False)
    body_template: Mapped[str] = mapped_column(String(10000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    sequence: Mapped[Sequence] = relationship(back_populates="steps")


class LeadSequence(Base):
    """Links a lead to a sequence, tracks progress."""

    __tablename__ = "lead_sequences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), nullable=False, unique=True)
    sequence_id: Mapped[int] = mapped_column(ForeignKey("sequences.id"), nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    lead: Mapped["Lead"] = relationship(back_populates="lead_sequence")
    sequence: Mapped[Sequence] = relationship()
    emails: Mapped[list["SequenceEmail"]] = relationship(back_populates="lead_sequence", order_by="SequenceEmail.id")


class SequenceEmailStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    sent = "sent"
    skipped = "skipped"


class SequenceEmail(Base):
    """One rendered email for a specific lead at a specific step."""

    __tablename__ = "sequence_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_sequence_id: Mapped[int] = mapped_column(ForeignKey("lead_sequences.id"), nullable=False)
    step_id: Mapped[int] = mapped_column(ForeignKey("sequence_steps.id"), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(String(10000), nullable=False)
    status: Mapped[SequenceEmailStatus] = mapped_column(
        _enum(SequenceEmailStatus, "seq_email_status"), nullable=False, default=SequenceEmailStatus.draft,
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lead_sequence: Mapped[LeadSequence] = relationship(back_populates="emails")
    step: Mapped[SequenceStep] = relationship()


class LeadActivity(Base):
    """Lightweight activity log for leads."""

    __tablename__ = "lead_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("crm_users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lead: Mapped["Lead"] = relationship(back_populates="activities")
    user: Mapped[CRMUser | None] = relationship()


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


class VerificationResult(Base):
    """One address's status as returned by an external verification service.

    Append-only like observations: a re-check of the same address on the same
    day from the same service is idempotent (unique on result_key); a later
    re-check is a new row, so the history is preserved. The engine reads the
    latest row per address.

    ``status`` is normalised to: deliverable | undeliverable | catch_all |
    unknown | no_mx | invalid. The service's own raw label is kept in
    ``raw_status`` for audit.
    """

    __tablename__ = "verification_results"
    __table_args__ = (UniqueConstraint("tenant_id", "result_key", name="uq_result_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    result_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    raw_status: Mapped[str | None] = mapped_column(String(64))
    catch_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score: Mapped[float | None] = mapped_column(Float)

    service: Mapped[str] = mapped_column(String(32), nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(255))
    checked_at: Mapped[date] = mapped_column(Date, nullable=False)
    extra: Mapped[dict | None] = mapped_column(JSON)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# Normalised verification statuses.
VERIFY_STATUSES = ("deliverable", "undeliverable", "catch_all", "unknown", "no_mx", "invalid")
# Statuses that confirm a real mailbox (feed delivery-confirmed evidence).
VERIFY_CONFIRMED = frozenset({"deliverable"})


# --- Append-only guard -----------------------------------------------------------


class AppendOnlyViolation(RuntimeError):
    pass


# Tables whose rows are immutable facts: no UPDATE, no DELETE from app code.
APPEND_ONLY = (EmailObservation, VerificationResult)
_APPEND_ONLY_TABLES = frozenset(m.__tablename__ for m in APPEND_ONLY)


@event.listens_for(Session, "before_flush")
def _block_observation_mutation(session: Session, flush_context, instances) -> None:
    for obj in session.deleted:
        if isinstance(obj, APPEND_ONLY):
            raise AppendOnlyViolation(f"{obj.__tablename__} is append-only: DELETE refused")
    for obj in session.dirty:
        if isinstance(obj, APPEND_ONLY) and session.is_modified(obj, include_collections=False):
            raise AppendOnlyViolation(f"{obj.__tablename__} is append-only: UPDATE refused")


@event.listens_for(Session, "do_orm_execute")
def _block_bulk_mutation(state) -> None:
    stmt = state.statement
    if isinstance(stmt, (Update, Delete)) and stmt.table.name in _APPEND_ONLY_TABLES:
        raise AppendOnlyViolation(f"{stmt.table.name} is append-only: bulk UPDATE/DELETE refused")
