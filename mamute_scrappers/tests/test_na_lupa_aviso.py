"""CS-137: aviso por e-mail a quem segue um parlamentar citado num caso do Na Lupa."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from mamute_scrappers.scripts.notificacao.na_lupa import (
    AVISO_NIVEL,
    MAX_TENTATIVAS,
    enviar,
    montar_avisos,
    nivel_da_pessoa,
    render_html,
)

AGORA = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
ONTEM = AGORA - timedelta(days=1)
MES_PASSADO = AGORA - timedelta(days=30)
ADMIN = "admin@mamute.com"

# Espelha a migration cs137a1b2c3d4 e as colunas das tabelas lidas.
DDL = [
    "CREATE TABLE projetos (id INTEGER PRIMARY KEY, email TEXT, nome TEXT, tier_id INTEGER, "
    "deleted_at TIMESTAMP, email_token TEXT, emails_desativados_em TIMESTAMP)",
    "CREATE TABLE parliamentarian (id INTEGER PRIMARY KEY, name TEXT, cpf TEXT)",
    "CREATE TABLE candidacy (id INTEGER PRIMARY KEY, parliamentarian_id INTEGER)",
    "CREATE TABLE projetos_parliamentarian (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "projeto_id INTEGER, parliamentarian_id INTEGER, created_at TIMESTAMP)",
    "CREATE TABLE collection (id INTEGER PRIMARY KEY, slug TEXT, title TEXT, subtitle TEXT, "
    "status TEXT, tier_labels JSON, settings JSON, published_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE collection_member (id INTEGER PRIMARY KEY, collection_id INTEGER, "
    "display_name TEXT, role_label TEXT, cpf TEXT, parliamentarian_id INTEGER, candidacy_id INTEGER, "
    "tier INTEGER, context TEXT, position INTEGER DEFAULT 0, created_at TIMESTAMP)",
    "CREATE TABLE feature_flag (key TEXT PRIMARY KEY, state TEXT NOT NULL)",
    "CREATE TABLE feature_flag_tier (flag_key TEXT, tier_id INTEGER, mode TEXT)",
    """CREATE TABLE na_lupa_notice (
        id INTEGER PRIMARY KEY AUTOINCREMENT, projeto_id INTEGER NOT NULL,
        collection_id INTEGER NOT NULL, parliamentarian_id INTEGER NOT NULL,
        member_id INTEGER, tier SMALLINT, tier_label TEXT,
        email_status TEXT NOT NULL DEFAULT 'pending', tentativas SMALLINT NOT NULL DEFAULT 0,
        ultimo_erro TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, sent_at TIMESTAMP,
        UNIQUE (projeto_id, collection_id, parliamentarian_id))""",
    "CREATE TABLE email_send_log (id INTEGER PRIMARY KEY AUTOINCREMENT, projeto_id BIGINT, "
    "email TEXT, periodicidade TEXT, status TEXT, detail TEXT, subject TEXT, stats JSON, "
    "period_start DATE, period_end DATE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
]

ROTULOS = {"1": "Investigados", "2": "Citados com fato concreto", "3": "Só contato salvo"}


class FakeMailer:
    def __init__(self, falhar: bool = False) -> None:
        self.enviados: List[Tuple[str, str, str]] = []
        self.falhar = falhar

    def __call__(self, body: str, to: str, subject: str) -> str:
        if self.falhar:
            raise RuntimeError("SMTP timeout")
        self.enviados.append((to, subject, body))
        return f"msg-{len(self.enviados)}"


def _sessao() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        for ddl in DDL:
            conn.exec_driver_sql(ddl)
    return Session(engine)


def _exec(sessao: Session, sql: str, **params) -> None:
    sessao.execute(text(sql), params)
    sessao.commit()


def _projeto(s: Session, pid: int, email: str, *, tier_id: int | None = 1, segue: tuple[int, ...] = ()) -> None:
    _exec(s, "INSERT INTO projetos (id, email, nome, tier_id, email_token) VALUES (:i, :e, :n, :t, :tok)",
          i=pid, e=email, n=email.split("@")[0], t=tier_id, tok=f"tok{pid}")
    for parl in segue:
        _exec(s, "INSERT INTO projetos_parliamentarian (projeto_id, parliamentarian_id) VALUES (:p, :parl)",
              p=pid, parl=parl)


def _caso(s: Session, cid: int, slug: str, *, status: str = "published", publicado_em=ONTEM, settings=None) -> None:
    _exec(s, "INSERT INTO collection (id, slug, title, subtitle, status, tier_labels, settings, published_at, updated_at) "
             "VALUES (:i, :slug, :t, :sub, :st, :tl, :se, :p, :p)",
          i=cid, slug=slug, t=f"Caso {slug}", sub="Quem é quem", st=status, tl=json.dumps(ROTULOS),
          se=json.dumps(settings or {}), p=publicado_em.isoformat() if publicado_em else None)


def _pessoa(s: Session, mid: int, cid: int, nome: str, *, parl: int | None = None, tier: int | None = 2,
            cpf: str | None = None, criado_em=ONTEM - timedelta(days=2), contexto: str | None = None) -> None:
    _exec(s, "INSERT INTO collection_member (id, collection_id, display_name, parliamentarian_id, cpf, tier, "
             "context, position, created_at) VALUES (:i, :c, :n, :p, :cpf, :t, :ctx, :i, :cr)",
          i=mid, c=cid, n=nome, p=parl, cpf=cpf, t=tier, ctx=contexto, cr=criado_em.isoformat())


def _flag(s: Session, estado: str, *planos: int) -> None:
    _exec(s, "INSERT INTO feature_flag (key, state) VALUES ('email_na_lupa', :e)", e=estado)
    for plano in planos:
        _exec(s, "INSERT INTO feature_flag_tier (flag_key, tier_id, mode) VALUES ('email_na_lupa', :t, 'liberado')",
              t=plano)


def _status(s: Session) -> dict:
    return {
        (r.projeto_id, r.collection_id, r.parliamentarian_id): r.email_status
        for r in s.execute(text("SELECT * FROM na_lupa_notice")).all()
    }


def _envia(s: Session, mailer: FakeMailer, **kw):
    return enviar(s, send=mailer, agora=AGORA, admins=frozenset({ADMIN}), **kw)


def _cenario() -> Session:
    """Caso master publicado ontem com Fulano (parl 1, nível 2) e Beltrano (parl 2, nível 1).
    Ana segue os dois; Bia segue só Beltrano; Caio segue um parlamentar fora do caso."""
    s = _sessao()
    _caso(s, 1, "master")
    _pessoa(s, 10, 1, "Fulano", parl=1, tier=2, contexto="Trocou mensagens com o banqueiro.")
    _pessoa(s, 11, 1, "Beltrano", parl=2, tier=1)
    _projeto(s, 100, "ana@x.com", segue=(1, 2))
    _projeto(s, 200, "bia@x.com", segue=(2,))
    _projeto(s, 300, "caio@x.com", segue=(9,))
    _flag(s, "all", 1)
    return s


def test_publicar_avisa_quem_segue_uma_vez_e_consolidado_por_caso() -> None:
    s = _cenario()
    mailer = FakeMailer()
    stats = _envia(s, mailer)

    assert stats.enviados == 2
    por_email = {to: (subject, body) for to, subject, body in mailer.enviados}
    assert set(por_email) == {"ana@x.com", "bia@x.com"}  # Caio não segue ninguém do caso
    assunto_ana, corpo_ana = por_email["ana@x.com"]
    assert assunto_ana == "2 parlamentares que você acompanha aparecem no caso “Caso master”, do Na Lupa"
    assert "Fulano" in corpo_ana and "Beltrano" in corpo_ana
    assert por_email["bia@x.com"][0] == "Beltrano aparece no caso “Caso master”, do Na Lupa"
    assert "Publicamos no Na Lupa o caso" in corpo_ana
    assert "/na-lupa/master" in corpo_ana
    assert set(_status(s).values()) == {"sent"}

    # Nova rodada (o cron roda a cada 15 min): ninguém recebe de novo.
    stats = _envia(s, mailer)
    assert stats.enviados == 0 and stats.ja_enviados == 2
    assert len(mailer.enviados) == 2

    log = s.execute(text("SELECT periodicidade, status FROM email_send_log")).all()
    assert {tuple(r) for r in log} == {("na_lupa_master", "sent")}


def test_texto_respeita_o_nivel_de_cada_pessoa() -> None:
    s = _cenario()
    mailer = FakeMailer()
    _envia(s, mailer)
    corpo = dict((to, body) for to, _, body in mailer.enviados)["ana@x.com"]

    assert "Nível 2: Citados com fato concreto" in corpo
    assert "Não quer dizer que seja investigado." in corpo  # explicação padrão do nível 2
    assert "Nível 1: Investigados" in corpo
    assert "Trocou mensagens com o banqueiro." in corpo
    assert "não quer dizer ser investigado" in corpo  # aviso no fim de todo e-mail
    assert AVISO_NIVEL.split(",")[0] in corpo


def test_explicacao_do_nivel_vem_do_caso_e_autoridade_nao_tem_nivel() -> None:
    settings = {"tier_descriptions": {"2": "Citado em depoimento."}}
    assert nivel_da_pessoa(2, ROTULOS, settings) == ("Nível 2: Citados com fato concreto", "Citado em depoimento.")
    # O caso trouxe explicações próprias: nível sem explicação fica sem, como na página.
    assert nivel_da_pessoa(1, ROTULOS, settings) == ("Nível 1: Investigados", None)
    assert nivel_da_pessoa(None, ROTULOS, settings) == ("Autoridade citada", None)
    assert nivel_da_pessoa(4, {}, {})[0] == "Nível 4: Nível 4"


def test_caso_em_rascunho_nao_dispara() -> None:
    s = _sessao()
    _caso(s, 1, "rascunho", status="draft")
    _pessoa(s, 10, 1, "Fulano", parl=1)
    _projeto(s, 100, "ana@x.com", segue=(1,))
    _flag(s, "all", 1)
    mailer = FakeMailer()
    assert _envia(s, mailer).enviados == 0
    assert mailer.enviados == []


def test_caso_antigo_so_dispara_com_desde() -> None:
    s = _sessao()
    _caso(s, 1, "antigo", publicado_em=MES_PASSADO)
    _pessoa(s, 10, 1, "Fulano", parl=1, criado_em=MES_PASSADO - timedelta(days=1))
    _projeto(s, 100, "ana@x.com", segue=(1,))
    _flag(s, "all", 1)
    mailer = FakeMailer()

    assert _envia(s, mailer).enviados == 0  # ligar a flag não reenvia casos velhos
    assert _envia(s, mailer, desde=MES_PASSADO - timedelta(days=1)).enviados == 1


def test_incluir_alguem_num_caso_publicado_avisa_so_sobre_a_pessoa_nova() -> None:
    s = _cenario()
    mailer = FakeMailer()
    _envia(s, mailer)
    mailer.enviados.clear()

    _pessoa(s, 12, 1, "Ciclano", parl=3, tier=3, criado_em=AGORA - timedelta(hours=1))
    _exec(s, "INSERT INTO projetos_parliamentarian (projeto_id, parliamentarian_id) VALUES (100, 3)")
    stats = _envia(s, mailer)

    assert stats.enviados == 1
    to, subject, body = mailer.enviados[0]
    assert to == "ana@x.com"
    assert subject == "Ciclano aparece no caso “Caso master”, do Na Lupa"
    assert "Atualizamos o caso" in body
    assert "Fulano" not in body  # já avisado antes
    assert "Nível 3: Só contato salvo" in body


def test_pessoa_ligada_so_pelo_cpf_tambem_avisa() -> None:
    s = _sessao()
    _exec(s, "INSERT INTO parliamentarian (id, name, cpf) VALUES (7, 'Dep', '12345678901')")
    _caso(s, 1, "cpf")
    _pessoa(s, 10, 1, "Dep", cpf="12345678901")
    _projeto(s, 100, "ana@x.com", segue=(7,))
    _flag(s, "all", 1)
    mailer = FakeMailer()
    assert _envia(s, mailer).enviados == 1


def test_flag_decide_quem_recebe() -> None:
    # off: ninguém, e nada reservado.
    s = _cenario()
    s.execute(text("UPDATE feature_flag SET state = 'off'"))
    s.commit()
    mailer = FakeMailer()
    assert _envia(s, mailer).enviados == 0 and _status(s) == {}

    # admins: só o e-mail de admin.
    s = _cenario()
    s.execute(text("UPDATE feature_flag SET state = 'admins'"))
    s.execute(text("UPDATE projetos SET email = :a WHERE id = 200"), {"a": ADMIN})
    s.commit()
    mailer = FakeMailer()
    stats = _envia(s, mailer)
    assert [to for to, _, _ in mailer.enviados] == [ADMIN]
    assert stats.fora_do_recorte == 1

    # all: só os planos liberados (Ana passa para um plano sem a flag).
    s = _cenario()
    s.execute(text("UPDATE projetos SET tier_id = 2 WHERE id = 100"))
    s.commit()
    mailer = FakeMailer()
    _envia(s, mailer)
    assert [to for to, _, _ in mailer.enviados] == ["bia@x.com"]
    # Ana fica sem linha: se o plano dela ganhar a flag dentro da janela, recebe.
    assert all(chave[0] != 100 for chave in _status(s))


def test_quem_pediu_para_parar_nao_recebe() -> None:
    s = _cenario()
    _exec(s, "UPDATE projetos SET emails_desativados_em = :d WHERE id = 100", d=ONTEM.isoformat())
    mailer = FakeMailer()
    stats = _envia(s, mailer)
    assert [to for to, _, _ in mailer.enviados] == ["bia@x.com"]
    assert stats.descadastrados == 1
    assert _status(s)[(100, 1, 1)] == "skipped_opt_out"


def test_falha_de_envio_tenta_de_novo_ate_o_limite() -> None:
    s = _cenario()
    quebrado = FakeMailer(falhar=True)
    for _ in range(MAX_TENTATIVAS):
        assert _envia(s, quebrado, projeto_id=200).erros == 1
    assert _status(s)[(200, 1, 2)] == "error"
    # Esgotou: para de tentar.
    assert _envia(s, quebrado, projeto_id=200).erros == 0

    # Ana ainda não tinha linha: recebe normalmente.
    ok = FakeMailer()
    _envia(s, ok, projeto_id=100)
    assert [to for to, _, _ in ok.enviados] == ["ana@x.com"]


def test_dry_run_nao_reserva_nem_envia(tmp_path) -> None:
    s = _cenario()
    mailer = FakeMailer()
    stats = _envia(s, mailer, dry_run=True, save_html_dir=tmp_path)
    assert stats.prontos == 2 and mailer.enviados == []
    assert _status(s) == {}
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "na_lupa_master_projeto_100.html",
        "na_lupa_master_projeto_200.html",
    ]


def test_render_escapa_texto_do_admin() -> None:
    s = _sessao()
    _caso(s, 1, "x")
    _pessoa(s, 10, 1, "<b>Fulano</b>", parl=1, contexto="<script>x</script>")
    _projeto(s, 100, "ana@x.com", segue=(1,))
    aviso = montar_avisos(s, desde=MES_PASSADO)[0]
    corpo = render_html(aviso)
    assert "<script>x</script>" not in corpo and "&lt;script&gt;" in corpo
    assert "<b>Fulano</b>" not in corpo
