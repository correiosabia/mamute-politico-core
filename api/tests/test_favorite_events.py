"""Add/remove de favorito gera usage_events (server-side)."""
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.routers.projects import _create_project_favorite, _delete_project_favorite


def _session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table tiers (id integer primary key, tier_name_debug text, "
            "product_id text, detalhes text, created_at datetime, updated_at datetime, deleted_at datetime)"
        )
        conn.exec_driver_sql(
            "create table projetos (id integer primary key, nome text, cliente text, "
            "email text, tier_id integer, tag_ghost text, qtd_termos integer default 0, "
            "created_at datetime, updated_at datetime, deleted_at datetime)"
        )
        conn.exec_driver_sql(
            """
            create table parliamentarian (
                id integer primary key, type text, parliamentarian_code integer,
                name text, full_name text, email text, telephone text, cpf text,
                status text, party text, state_of_birth text, city_of_birth text,
                state_elected text, site text, education text, office_name text,
                office_building text, office_number text, office_floor text,
                office_email text, biography_link text, biography_text text,
                details text, created_at datetime not null, updated_at datetime not null
            )
            """
        )
        conn.exec_driver_sql(
            "create table projetos_parliamentarian (id integer primary key, projeto_id integer, "
            "parliamentarian_id integer, position integer, created_at datetime, "
            "updated_at datetime, deleted_at datetime)"
        )
        conn.exec_driver_sql(
            """create table usage_events (id integer primary key, projeto_id integer,
               email text, event_type text not null, page text, section text,
               parliamentarian_id integer, country text, region text, city text, lat real, lon real, created_at datetime default current_timestamp)"""
        )
        conn.exec_driver_sql(
            "insert into projetos (id, nome, email, qtd_termos) values (1, 'Ana', 'ana@x.com', 10)"
        )
        conn.exec_driver_sql(
            "insert into parliamentarian (id, name, created_at, updated_at) "
            "values (10, 'Dep X', current_timestamp, current_timestamp)"
        )
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_favorite_add_and_remove_log_events() -> None:
    db = _session()
    _create_project_favorite(db, 1, 10)
    _delete_project_favorite(db, 1, 10)

    rows = db.execute(
        text("select event_type, projeto_id, parliamentarian_id from usage_events order by id")
    ).all()
    assert [r[0] for r in rows] == ["favorite_added", "favorite_removed"]
    assert rows[0] == ("favorite_added", 1, 10)
    db.close()
