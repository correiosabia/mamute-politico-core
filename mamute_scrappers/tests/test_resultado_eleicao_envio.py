"""CS-106/CS-119: aviso do resultado (disparos globais, flag, sem duplicata, 2o turno)."""

from __future__ import annotations

import json
from typing import Iterable, List, Tuple

from sqlalchemy import text

from eleicao_schema import (
    add_candidacy,
    add_projeto,
    follow,
    liberar_no_plano,
    make_session,
    set_flag,
)
from mamute_scrappers.scripts.notificacao.resultado_eleicao import (
    CARGOS_MAJORITARIOS,
    CARGOS_TODOS,
    DISPARO_COMPLETO,
    DISPARO_MAJORITARIOS,
    NAO_CONSTA,
    assunto,
    enviar,
    montar_avisos,
)
from mamute_scrappers.tse_crawler.resultados_parsing import abrangencias

CICLO = "ele2026"
DEPUTADOS = CARGOS_TODOS - CARGOS_MAJORITARIOS
ASSUNTO_MAJ = assunto(1, DISPARO_MAJORITARIOS)
ASSUNTO_COMPLETO = assunto(1, DISPARO_COMPLETO)


class FakeMailer:
    def __init__(self, falhar_para: set[str] | None = None) -> None:
        self.enviados: List[Tuple[str, str]] = []
        self.falhar_para = falhar_para or set()

    def __call__(self, body: str, to: str, subject: str) -> None:
        if to in self.falhar_para:
            raise RuntimeError("SMTP timeout")
        assert "<html" in body
        self.enviados.append((to, subject))


def _fechar(
    session,
    cargos: Iterable[int],
    *,
    turno: int = 1,
    exceto: set[tuple[int, str]] = frozenset(),
    ufs: dict[int, tuple[str, ...]] | None = None,
) -> None:
    """Grava os arquivos do TSE desses cargos como encerrados (todas as UFs)."""
    codigo = 6259 if turno == 1 else 6260
    for cargo in cargos:
        for uf in (ufs or {}).get(cargo, abrangencias(cargo)):
            session.execute(
                text(
                    "INSERT INTO tse_result_file (ciclo, codigo_eleicao, turno, uf, cargo_codigo, totalizacao_final) "
                    "VALUES (:c, :cod, :t, :uf, :cargo, :f) "
                    "ON CONFLICT (codigo_eleicao, uf, cargo_codigo) DO UPDATE SET totalizacao_final = excluded.totalizacao_final"
                ),
                {"c": CICLO, "cod": codigo, "t": turno, "uf": uf, "cargo": cargo, "f": (cargo, uf) not in exceto},
            )
    session.commit()


def _resultado(session, candidacy_id: int, situacao: str, *, turno: int = 1, votos: int = 1000) -> None:
    session.execute(
        text(
            "INSERT INTO candidacy_result (candidacy_id, turno, codigo_eleicao, situacao, eleito, "
            "votos, percentual, totalizacao_final) VALUES (:id, :t, 6259, :s, :e, :v, 12.5, 1)"
        ),
        {"id": candidacy_id, "t": turno, "s": situacao, "e": situacao.startswith("Eleito"), "v": votos},
    )
    session.commit()


def _cenario():
    """Ana (10) acompanha governador SP (2o turno) e dep. federal SP (eleito).
    Bia (20) acompanha so dep. federal RJ. Caio (30) acompanha so presidente."""
    s = make_session()
    add_projeto(s, 10, "ana@x.com")
    add_projeto(s, 20, "bia@x.com")
    add_projeto(s, 30, "caio@x.com")
    add_candidacy(s, 1, 111, office_code=3, state="SP", name="GOV SP")
    add_candidacy(s, 2, 222, office_code=6, state="SP", name="DEP SP")
    add_candidacy(s, 3, 333, office_code=6, state="RJ", name="DEP RJ")
    add_candidacy(s, 5, 555, office_code=1, state="BR", name="PRESIDENTE")
    follow(s, 10, 1)
    follow(s, 10, 2)
    follow(s, 20, 3)
    follow(s, 30, 5)
    _resultado(s, 1, "2º turno")
    _resultado(s, 2, "Eleito por média")
    _resultado(s, 3, "Suplente")
    _resultado(s, 5, "2º turno")
    liberar_no_plano(s, 1)  # projetos nascem no plano 1 (eleicao_schema)
    return s


def _payloads(s) -> dict[tuple[int, int, str], dict]:
    rows = s.execute(text("SELECT projeto_id, turno, disparo, payload FROM election_result_notice")).all()
    return {(r.projeto_id, r.turno, r.disparo): json.loads(r.payload) for r in rows}


def test_nada_sai_enquanto_falta_uma_uf_dos_majoritarios() -> None:
    s = _cenario()
    _fechar(s, CARGOS_MAJORITARIOS, exceto={(5, "ac")})  # senado do AC aberto
    prontos, pendentes = montar_avisos(s, ciclo=CICLO, turno=1, disparo=DISPARO_MAJORITARIOS)
    assert prontos == [] and pendentes == 1


def test_majoritarios_saem_antes_dos_deputados_so_para_quem_acompanha_algum() -> None:
    s = _cenario()
    set_flag(s, "all")
    _fechar(s, CARGOS_MAJORITARIOS)
    mailer = FakeMailer()
    stats = enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    assert sorted(mailer.enviados) == [("ana@x.com", ASSUNTO_MAJ), ("caio@x.com", ASSUNTO_MAJ)]
    assert stats.aguardando > 0  # deputados ainda abertos
    payloads = _payloads(s)
    assert [i["nome"] for i in payloads[(10, 1, DISPARO_MAJORITARIOS)]["itens"]] == ["GOV SP"]
    assert payloads[(10, 1, DISPARO_MAJORITARIOS)]["disparo"] == DISPARO_MAJORITARIOS


def test_completo_traz_todos_os_acompanhados_para_todo_mundo() -> None:
    s = _cenario()
    set_flag(s, "all")
    _fechar(s, CARGOS_MAJORITARIOS)
    mailer = FakeMailer()
    enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    mailer.enviados.clear()

    _fechar(s, DEPUTADOS)
    enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    assert sorted(mailer.enviados) == [
        ("ana@x.com", ASSUNTO_COMPLETO),
        ("bia@x.com", ASSUNTO_COMPLETO),
        ("caio@x.com", ASSUNTO_COMPLETO),
    ]
    itens = {i["nome"]: i["situacao"] for i in _payloads(s)[(10, 1, DISPARO_COMPLETO)]["itens"]}
    assert itens == {"GOV SP": "2º turno", "DEP SP": "Eleito por média"}  # texto do TSE
    assert _payloads(s)[(20, 1, DISPARO_COMPLETO)]["itens"][0]["situacao"] == "Suplente"


def test_tudo_fechado_de_uma_vez_manda_so_o_completo() -> None:
    s = _cenario()
    set_flag(s, "all")
    _fechar(s, CARGOS_TODOS)
    mailer = FakeMailer()
    enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    assert sorted(mailer.enviados) == [
        ("ana@x.com", ASSUNTO_COMPLETO),
        ("bia@x.com", ASSUNTO_COMPLETO),
        ("caio@x.com", ASSUNTO_COMPLETO),
    ]
    assert {k[2] for k in _payloads(s)} == {DISPARO_COMPLETO}


def test_rodar_duas_vezes_envia_uma() -> None:
    s = _cenario()
    set_flag(s, "all")
    _fechar(s, CARGOS_MAJORITARIOS)
    mailer = FakeMailer()
    enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    stats = enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    assert len(mailer.enviados) == 2
    assert stats.ja_enviados == 2

    row = s.execute(
        text("SELECT email_status, tentativas, sent_at FROM election_result_notice WHERE projeto_id = 10")
    ).one()
    assert row.email_status == "sent" and row.tentativas == 1 and row.sent_at is not None
    log = s.execute(text("SELECT DISTINCT periodicidade, status FROM email_send_log")).all()
    assert [(r.periodicidade, r.status) for r in log] == [("eleicao_ele2026_t1_majoritarios", "sent")]


def test_candidato_ausente_de_arquivo_encerrado_nao_trava() -> None:
    s = _cenario()
    add_candidacy(s, 4, 444, office_code=6, state="SP", name="INDEFERIDO")
    follow(s, 10, 4)
    _fechar(s, CARGOS_TODOS)
    prontos, _ = montar_avisos(s, ciclo=CICLO, turno=1, disparo=DISPARO_COMPLETO)
    ana = next(a for a in prontos if a.projeto_id == 10)
    assert {i["nome"]: i["situacao"] for i in ana.itens}["INDEFERIDO"] == NAO_CONSTA


def test_flag_off_nao_envia() -> None:
    s = _cenario()
    _fechar(s, CARGOS_TODOS)
    mailer = FakeMailer()
    stats = enviar(s, ciclo=CICLO, send=mailer)
    assert stats.flag == "off" and mailer.enviados == []
    assert s.execute(text("SELECT count(*) FROM election_result_notice")).scalar() == 0


def test_flag_admins_so_envia_para_admin() -> None:
    s = _cenario()
    set_flag(s, "admins")
    _fechar(s, CARGOS_TODOS)
    mailer = FakeMailer()
    enviar(s, ciclo=CICLO, send=mailer, admins=frozenset({"ana@x.com"}))
    assert mailer.enviados == [("ana@x.com", ASSUNTO_COMPLETO)]


def test_flag_all_respeita_o_plano_igual_ao_modal() -> None:
    """Mesma regra do resolve_for da API: em `all`, quem decide e o plano."""
    s = _cenario()
    set_flag(s, "all")
    _fechar(s, CARGOS_TODOS)
    s.execute(text("UPDATE projetos SET tier_id = 2"))  # plano sem a flag
    s.commit()
    mailer = FakeMailer()
    stats = enviar(s, ciclo=CICLO, send=mailer, admins=frozenset())
    assert mailer.enviados == [] and stats.fora_do_recorte == 3

    # admin recebe mesmo em plano sem a flag (previa e conferencia)
    enviar(s, ciclo=CICLO, send=mailer, admins=frozenset({"ana@x.com"}))
    assert [to for to, _ in mailer.enviados] == ["ana@x.com"]


def test_falha_de_smtp_tenta_de_novo_ate_o_limite() -> None:
    s = _cenario()
    set_flag(s, "all")
    _fechar(s, CARGOS_TODOS)
    mailer = FakeMailer(falhar_para={"ana@x.com"})
    for _ in range(5):
        enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    row = s.execute(
        text("SELECT email_status, tentativas, ultimo_erro FROM election_result_notice WHERE projeto_id = 10")
    ).one()
    assert row.email_status == "error" and row.tentativas == 3
    assert "SMTP" in row.ultimo_erro

    mailer.falhar_para.clear()
    s.execute(text("UPDATE election_result_notice SET tentativas = 1 WHERE projeto_id = 10"))
    s.commit()
    mailer.enviados.clear()
    enviar(s, ciclo=CICLO, send=mailer, turnos=(1,))
    assert [to for to, _ in mailer.enviados] == ["ana@x.com"]


def test_segundo_turno_espera_todos_os_arquivos_do_segundo_turno() -> None:
    """Governador SP e presidente foram ao 2o turno: os dois arquivos precisam fechar."""
    s = _cenario()
    _fechar(s, CARGOS_TODOS)
    _fechar(s, {3}, turno=2, ufs={3: ("sp",)})
    prontos, pendentes = montar_avisos(s, ciclo=CICLO, turno=2)
    assert prontos == [] and pendentes == 1  # falta o de presidente (br)


def test_segundo_turno_so_para_quem_acompanha_quem_foi_ao_segundo_turno() -> None:
    s = _cenario()
    set_flag(s, "all")
    _fechar(s, CARGOS_TODOS)
    mailer = FakeMailer()
    enviar(s, ciclo=CICLO, send=mailer)
    mailer.enviados.clear()

    _fechar(s, {1, 3}, turno=2, ufs={1: ("br",), 3: ("sp",)})
    _resultado(s, 1, "Eleito", turno=2)
    _resultado(s, 5, "Não eleito", turno=2)
    enviar(s, ciclo=CICLO, send=mailer)
    assunto_t2 = assunto(2)
    assert sorted(mailer.enviados) == [("ana@x.com", assunto_t2), ("caio@x.com", assunto_t2)]
    payload = _payloads(s)[(10, 2, DISPARO_COMPLETO)]
    assert [i["nome"] for i in payload["itens"]] == ["GOV SP"]
    assert payload["itens"][0]["situacao"] == "Eleito"


def test_segundo_turno_nao_sai_antes_do_primeiro_ter_resultado() -> None:
    s = make_session()
    prontos, pendentes = montar_avisos(s, ciclo=CICLO, turno=2)
    assert prontos == [] and pendentes == 0
