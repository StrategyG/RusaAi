"""Create the initial RUSA-AI agent schema.

Revision ID: 0001_agent
Revises:
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_agent"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("repository", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("work_branch", sa.String(length=255), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("require_ci", sa.Boolean(), nullable=False),
        sa.Column("require_pull_request", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=True),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("tool_results", sa.JSON(), nullable=False),
        sa.Column("approved_actions", sa.JSON(), nullable=False),
        sa.Column("pending_action", sa.String(length=128), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_agent_task_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_agent_tasks_claim",
        "agent_tasks",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_agent_tasks_tenant_created",
        "agent_tasks",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "agent_audit_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("event", sa.String(length=128), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agent_audit_tenant_task",
        "agent_audit_events",
        ["tenant_id", "task_id", "created_at"],
    )

    op.create_table(
        "agent_memory",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("namespace", sa.String(length=128), nullable=False),
        sa.Column("memory_key", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "namespace",
            "memory_key",
            name="uq_agent_memory_scope_key",
        ),
    )
    op.create_index(
        "ix_agent_memory_tenant_namespace",
        "agent_memory",
        ["tenant_id", "namespace"],
    )

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("from_agent", sa.String(length=32), nullable=False),
        sa.Column("to_agent", sa.String(length=32), nullable=False),
        sa.Column("message_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agent_messages_tenant_task_created",
        "agent_messages",
        ["tenant_id", "task_id", "created_at"],
    )
    op.create_index(
        "ix_agent_messages_correlation",
        "agent_messages",
        ["correlation_id"],
    )


    op.execute("""
        CREATE TABLE agent_documents (
            id varchar(36) PRIMARY KEY,
            tenant_id varchar(128) NOT NULL,
            source_id varchar(255) NOT NULL,
            chunk_id varchar(64) NOT NULL,
            title varchar(500) NOT NULL,
            content text NOT NULL,
            metadata_json json NOT NULL DEFAULT '{}'::json,
            embedding vector(384) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_agent_documents_tenant_source_chunk
                UNIQUE (tenant_id, source_id, chunk_id)
        )
    """)
    op.create_index(
        "ix_agent_documents_tenant_source",
        "agent_documents",
        ["tenant_id", "source_id"],
    )
    op.execute(
        "CREATE INDEX ix_agent_documents_embedding ON agent_documents "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index("ix_agent_documents_embedding", table_name="agent_documents")
    op.drop_index("ix_agent_documents_tenant_source", table_name="agent_documents")
    op.drop_table("agent_documents")
    op.drop_index("ix_agent_messages_correlation", table_name="agent_messages")
    op.drop_index("ix_agent_messages_tenant_task_created", table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_index("ix_agent_memory_tenant_namespace", table_name="agent_memory")
    op.drop_table("agent_memory")
    op.drop_index("ix_agent_audit_tenant_task", table_name="agent_audit_events")
    op.drop_table("agent_audit_events")
    op.drop_index("ix_agent_tasks_tenant_created", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_claim", table_name="agent_tasks")
    op.drop_table("agent_tasks")
