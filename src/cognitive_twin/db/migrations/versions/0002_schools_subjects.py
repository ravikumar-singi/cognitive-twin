"""Schools, class codes, local accounts, age groups and several subjects

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 09:00:00

Existing data is kept: every existing user and class moves into one school named
after the pilot's partner institution (single sign-on, adults only, as phase one was), existing learner
state becomes their "ml-foundations" subject, and existing adults keep their
verified status as the "adult" age group.
"""
import uuid

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

DEFAULT_TRACK = "ml-foundations"


def upgrade() -> None:
    op.create_table('schools',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('sign_in', sa.String(length=8), nullable=False),
    sa.Column('region', sa.String(length=8), nullable=False),
    sa.Column('youngest_band', sa.String(length=10), nullable=False),
    sa.Column('allowed_tracks', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )

    # users: school, age group, guardian consent
    op.add_column('users', sa.Column('school_id', sa.String(length=36), nullable=True))
    op.add_column('users', sa.Column('age_band', sa.String(length=10),
                                     server_default='unknown', nullable=False))
    op.add_column('users', sa.Column('guardian_ok', sa.Boolean(),
                                     server_default=sa.false(), nullable=False))
    op.alter_column('users', 'email', existing_type=sa.String(length=320), nullable=True)
    op.create_index(op.f('ix_users_school_id'), 'users', ['school_id'], unique=False)
    op.create_foreign_key('users_school_id_fkey', 'users', 'schools', ['school_id'], ['id'])

    # cohorts become classes inside a school, with a subject and a join code
    op.add_column('cohorts', sa.Column('school_id', sa.String(length=36), nullable=True))
    op.add_column('cohorts', sa.Column('track_id', sa.String(length=60), nullable=True))
    op.add_column('cohorts', sa.Column('join_code', sa.String(length=16), nullable=True))

    conn = op.get_bind()
    has_data = conn.execute(sa.text(
        "SELECT (SELECT count(*) FROM users) + (SELECT count(*) FROM cohorts)")).scalar()
    if has_data:
        sid = str(uuid.uuid4())
        # named after the partner institution, so sign-ins keep landing in the same school
        name = conn.execute(sa.text(
            "SELECT institution FROM users WHERE institution IS NOT NULL "
            "GROUP BY institution ORDER BY count(*) DESC LIMIT 1")).scalar() or "Pilot school"
        conn.execute(sa.text(
            "INSERT INTO schools (id, name, sign_in, region, youngest_band, created_at) "
            "VALUES (:id, :name, 'sso', 'IN', 'adult', now())"), {"id": sid, "name": name[:120]})
        conn.execute(sa.text("UPDATE cohorts SET school_id = :id"), {"id": sid})
        conn.execute(sa.text("UPDATE users SET school_id = :id WHERE role <> 'admin'"),
                     {"id": sid})
    conn.execute(sa.text("UPDATE users SET age_band = 'adult' WHERE adult_verified"))
    conn.execute(sa.text("UPDATE users SET age_band = 'adult' WHERE role <> 'learner'"))

    op.alter_column('cohorts', 'school_id', existing_type=sa.String(length=36), nullable=False)
    op.drop_constraint('cohorts_name_key', 'cohorts', type_='unique')
    op.create_unique_constraint('uq_cohort_school_name', 'cohorts', ['school_id', 'name'])
    op.create_unique_constraint('cohorts_join_code_key', 'cohorts', ['join_code'])
    op.create_foreign_key('cohorts_school_id_fkey', 'cohorts', 'schools', ['school_id'], ['id'],
                          ondelete='CASCADE')

    # one twin per learner per subject
    op.add_column('twins', sa.Column('track_id', sa.String(length=60),
                                     server_default=DEFAULT_TRACK, nullable=False))
    op.drop_constraint('twins_pkey', 'twins', type_='primary')
    op.create_primary_key('twins_pkey', 'twins', ['user_id', 'track_id'])
    for table in ('attempts', 'decisions', 'guardian_interventions', 'policy_versions',
                  'policy_activations'):
        op.add_column(table, sa.Column('track_id', sa.String(length=60),
                                       server_default=DEFAULT_TRACK, nullable=False))

    op.create_table('local_credentials',
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('username', sa.String(length=40), nullable=False),
    sa.Column('password_hash', sa.String(length=200), nullable=False),
    sa.Column('must_change', sa.Boolean(), nullable=False),
    sa.Column('failed_attempts', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('session_epoch', sa.Integer(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id'),
    sa.UniqueConstraint('username')
    )
    op.create_table('invites',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('school_id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.String(length=36), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_hash')
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0002 is not reversible without losing school, class and subject data; "
        "restore the pre-upgrade backup instead (see docs/production.md)")
