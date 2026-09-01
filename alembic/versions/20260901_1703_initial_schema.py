"""initial schema

Revision ID: e213d8fda426
Revises: 
Create Date: 2026-09-01 17:03:40.443845
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e213d8fda426'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pg_trgm backs the GIN index used by the ?search= substring filter on
    # ipos.name. It ships with PostgreSQL; creating it here keeps the database
    # setup fully automatic, with no manual step after `docker compose up`.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table('ipos',
    sa.Column('source', sa.String(length=40), server_default='investorgain', nullable=False),
    sa.Column('source_ipo_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('symbol', sa.String(length=40), nullable=True),
    sa.Column('slug', sa.String(length=255), nullable=True),
    sa.Column('detail_url', sa.Text(), nullable=True),
    sa.Column('ipo_type', sa.String(length=20), server_default='UNKNOWN', nullable=False),
    sa.Column('exchange', sa.String(length=20), server_default='UNKNOWN', nullable=False),
    sa.Column('source_status', sa.String(length=20), nullable=True),
    sa.Column('open_date', sa.Date(), nullable=True),
    sa.Column('close_date', sa.Date(), nullable=True),
    sa.Column('allotment_date', sa.Date(), nullable=True),
    sa.Column('listing_date', sa.Date(), nullable=True),
    sa.Column('price_min', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('price_max', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('lot_size', sa.Integer(), nullable=True),
    sa.Column('issue_size_crore', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('gmp', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('gmp_percentage', sa.Numeric(precision=9, scale=2), nullable=True),
    sa.Column('gmp_low', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('gmp_high', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('estimated_listing_price', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('subscription_times', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('rating', sa.Integer(), nullable=True),
    sa.Column('pe_ratio', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('has_anchor_investors', sa.Boolean(), nullable=True),
    sa.Column('raw_data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('source_updated_text', sa.String(length=64), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_scraped_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('data_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('lot_size IS NULL OR lot_size > 0', name=op.f('ck_ipos_lot_size_positive')),
    sa.CheckConstraint('open_date IS NULL OR close_date IS NULL OR open_date <= close_date', name=op.f('ck_ipos_open_before_close')),
    sa.CheckConstraint('price_min IS NULL OR price_max IS NULL OR price_min <= price_max', name=op.f('ck_ipos_price_band_ordered')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ipos')),
    sa.UniqueConstraint('source', 'source_ipo_id', name='uq_ipos_source_source_ipo_id')
    )
    op.create_index('ix_ipos_close_date_gmp_percentage', 'ipos', ['close_date', 'gmp_percentage'], unique=False)
    op.create_index(op.f('ix_ipos_created_at'), 'ipos', ['created_at'], unique=False)
    op.create_index('ix_ipos_exchange', 'ipos', ['exchange'], unique=False)
    op.create_index('ix_ipos_gmp_percentage', 'ipos', ['gmp_percentage'], unique=False)
    op.create_index('ix_ipos_ipo_type_close_date', 'ipos', ['ipo_type', 'close_date'], unique=False)
    op.create_index('ix_ipos_listing_date', 'ipos', ['listing_date'], unique=False)
    op.create_index('ix_ipos_name_trgm', 'ipos', ['name'], unique=False, postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.create_index('ix_ipos_open_date', 'ipos', ['open_date'], unique=False)
    op.create_table('scrape_runs',
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('strategy', sa.String(length=20), server_default='NONE', nullable=False),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('records_found', sa.Integer(), server_default='0', nullable=False),
    sa.Column('records_valid', sa.Integer(), server_default='0', nullable=False),
    sa.Column('records_invalid', sa.Integer(), server_default='0', nullable=False),
    sa.Column('ipos_inserted', sa.Integer(), server_default='0', nullable=False),
    sa.Column('ipos_updated', sa.Integer(), server_default='0', nullable=False),
    sa.Column('ipos_unchanged', sa.Integer(), server_default='0', nullable=False),
    sa.Column('snapshots_created', sa.Integer(), server_default='0', nullable=False),
    sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('field_mapping', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('confidence IS NULL OR (confidence >= 0 AND confidence <= 1)', name=op.f('ck_scrape_runs_confidence_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scrape_runs'))
    )
    op.create_index(op.f('ix_scrape_runs_created_at'), 'scrape_runs', ['created_at'], unique=False)
    op.create_index('ix_scrape_runs_started_at_status', 'scrape_runs', ['started_at', 'status'], unique=False)
    op.create_table('users',
    sa.Column('phone_number', sa.String(length=20), nullable=False),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("phone_number ~ '^\\+[1-9][0-9]{6,17}$'", name=op.f('ck_users_phone_number_e164')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('phone_number', name=op.f('uq_users_phone_number'))
    )
    op.create_index(op.f('ix_users_created_at'), 'users', ['created_at'], unique=False)
    op.create_index('uq_users_email', 'users', ['email'], unique=True, postgresql_where=sa.text('email IS NOT NULL'))
    op.create_table('devices',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('device_type', sa.String(length=20), nullable=False),
    sa.Column('push_token', sa.Text(), nullable=False),
    sa.Column('device_name', sa.String(length=120), nullable=True),
    sa.Column('app_version', sa.String(length=40), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_devices_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_devices')),
    sa.UniqueConstraint('push_token', name=op.f('uq_devices_push_token'))
    )
    op.create_index(op.f('ix_devices_created_at'), 'devices', ['created_at'], unique=False)
    op.create_index('ix_devices_user_id_active', 'devices', ['user_id'], unique=False, postgresql_where=sa.text('is_active'))
    op.create_table('ipo_snapshots',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('ipo_id', sa.UUID(), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('gmp', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('gmp_percentage', sa.Numeric(precision=9, scale=2), nullable=True),
    sa.Column('subscription_times', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('price_min', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('price_max', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('lot_size', sa.Integer(), nullable=True),
    sa.Column('issue_size_crore', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('open_date', sa.Date(), nullable=True),
    sa.Column('close_date', sa.Date(), nullable=True),
    sa.Column('listing_date', sa.Date(), nullable=True),
    sa.Column('source_status', sa.String(length=20), nullable=True),
    sa.Column('changed_fields', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('scrape_run_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ipo_id'], ['ipos.id'], name=op.f('fk_ipo_snapshots_ipo_id_ipos'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['scrape_run_id'], ['scrape_runs.id'], name=op.f('fk_ipo_snapshots_scrape_run_id_scrape_runs'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ipo_snapshots'))
    )
    op.create_index(op.f('ix_ipo_snapshots_created_at'), 'ipo_snapshots', ['created_at'], unique=False)
    op.create_index('ix_ipo_snapshots_ipo_id_captured_at', 'ipo_snapshots', ['ipo_id', 'captured_at'], unique=False)
    op.create_table('notification_preferences',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('label', sa.String(length=80), nullable=True),
    sa.Column('is_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('min_gmp_percentage', sa.Numeric(precision=9, scale=2), server_default='0', nullable=False),
    sa.Column('max_gmp_percentage', sa.Numeric(precision=9, scale=2), nullable=True),
    sa.Column('interval_minutes', sa.Integer(), server_default='180', nullable=False),
    sa.Column('only_on_close_date', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('ipo_types', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('exchanges', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('min_subscription_times', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('channels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'["PUSH"]\'::jsonb'), nullable=False),
    sa.Column('extra_conditions', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('last_evaluated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('interval_minutes >= 15 AND interval_minutes <= 10080', name=op.f('ck_notification_preferences_interval_minutes_range')),
    sa.CheckConstraint('max_gmp_percentage IS NULL OR max_gmp_percentage >= min_gmp_percentage', name=op.f('ck_notification_preferences_gmp_percentage_range_ordered')),
    sa.CheckConstraint('min_gmp_percentage >= -100 AND min_gmp_percentage <= 1000', name=op.f('ck_notification_preferences_min_gmp_percentage_range')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_notification_preferences_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_preferences'))
    )
    op.create_index(op.f('ix_notification_preferences_created_at'), 'notification_preferences', ['created_at'], unique=False)
    op.create_index('ix_notification_preferences_enabled', 'notification_preferences', ['user_id'], unique=False, postgresql_where=sa.text('is_enabled'))
    op.create_table('scrape_raw_payloads',
    sa.Column('scrape_run_id', sa.UUID(), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('content_type', sa.String(length=64), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('byte_size', sa.Integer(), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.ForeignKeyConstraint(['scrape_run_id'], ['scrape_runs.id'], name=op.f('fk_scrape_raw_payloads_scrape_run_id_scrape_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scrape_raw_payloads'))
    )
    op.create_index('ix_scrape_raw_payloads_captured_at', 'scrape_raw_payloads', ['captured_at'], unique=False)
    op.create_index('ix_scrape_raw_payloads_scrape_run_id', 'scrape_raw_payloads', ['scrape_run_id'], unique=False)
    op.create_table('notification_deliveries',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('preference_id', sa.UUID(), nullable=False),
    sa.Column('ipo_id', sa.UUID(), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('period_key', sa.BigInteger(), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('gmp_percentage_at_send', sa.Numeric(precision=9, scale=2), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=True),
    sa.Column('provider_message_id', sa.String(length=255), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ipo_id'], ['ipos.id'], name=op.f('fk_notification_deliveries_ipo_id_ipos'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['preference_id'], ['notification_preferences.id'], name=op.f('fk_notification_deliveries_preference_id_notification_preferences'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_notification_deliveries_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_deliveries')),
    sa.UniqueConstraint('preference_id', 'ipo_id', 'period_key', name='uq_notification_delivery_period')
    )
    op.create_index(op.f('ix_notification_deliveries_created_at'), 'notification_deliveries', ['created_at'], unique=False)
    op.create_index('ix_notification_deliveries_status', 'notification_deliveries', ['status'], unique=False)
    op.create_index('ix_notification_deliveries_user_id_created_at', 'notification_deliveries', ['user_id', 'created_at'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_notification_deliveries_user_id_created_at', table_name='notification_deliveries')
    op.drop_index('ix_notification_deliveries_status', table_name='notification_deliveries')
    op.drop_index(op.f('ix_notification_deliveries_created_at'), table_name='notification_deliveries')
    op.drop_table('notification_deliveries')
    op.drop_index('ix_scrape_raw_payloads_scrape_run_id', table_name='scrape_raw_payloads')
    op.drop_index('ix_scrape_raw_payloads_captured_at', table_name='scrape_raw_payloads')
    op.drop_table('scrape_raw_payloads')
    op.drop_index('ix_notification_preferences_enabled', table_name='notification_preferences', postgresql_where=sa.text('is_enabled'))
    op.drop_index(op.f('ix_notification_preferences_created_at'), table_name='notification_preferences')
    op.drop_table('notification_preferences')
    op.drop_index('ix_ipo_snapshots_ipo_id_captured_at', table_name='ipo_snapshots')
    op.drop_index(op.f('ix_ipo_snapshots_created_at'), table_name='ipo_snapshots')
    op.drop_table('ipo_snapshots')
    op.drop_index('ix_devices_user_id_active', table_name='devices', postgresql_where=sa.text('is_active'))
    op.drop_index(op.f('ix_devices_created_at'), table_name='devices')
    op.drop_table('devices')
    op.drop_index('uq_users_email', table_name='users', postgresql_where=sa.text('email IS NOT NULL'))
    op.drop_index(op.f('ix_users_created_at'), table_name='users')
    op.drop_table('users')
    op.drop_index('ix_scrape_runs_started_at_status', table_name='scrape_runs')
    op.drop_index(op.f('ix_scrape_runs_created_at'), table_name='scrape_runs')
    op.drop_table('scrape_runs')
    op.drop_index('ix_ipos_open_date', table_name='ipos')
    op.drop_index('ix_ipos_name_trgm', table_name='ipos', postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.drop_index('ix_ipos_listing_date', table_name='ipos')
    op.drop_index('ix_ipos_ipo_type_close_date', table_name='ipos')
    op.drop_index('ix_ipos_gmp_percentage', table_name='ipos')
    op.drop_index('ix_ipos_exchange', table_name='ipos')
    op.drop_index(op.f('ix_ipos_created_at'), table_name='ipos')
    op.drop_index('ix_ipos_close_date_gmp_percentage', table_name='ipos')
    op.drop_table('ipos')
    # pg_trgm is deliberately not dropped: other schemas in the same database
    # may rely on it, and re-creating it is cheap.
    # ### end Alembic commands ###
