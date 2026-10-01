"""CS-106: aviso do resultado (quem fica pronto, flag, sem duplicata, 2o turno)."""

from __future__ import annotations

import json
from typing import List, Tuple

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
    NAO_CONSTA,
    enviar,
    montar_avisos,
    render_html,
)

CICLO = "ele2026"


class FakeMailer:
    def __init__(self, falhar_para: set[str] | None = None) -> None:
        self.enviados: List[Tuple[str, str]] = []
        self.falhar_para = falhar_para or set()

    def __call__(self, body: str, to: str, subject: str) -> None:
        if to in self.falhar_para:
            raise RuntimeError("SMTP timeout")
        assert "<html" in body
        self.enviados.append((to, subject))


def _arquivo(session, cargo: int, uf: str, *, final: bool = True, turno: int = 1, codigo: int = 6259) -> None:
    session.execute(
        text(
            "INSERT INTO tse_result_file (ciclo, codigo_eleicao, turno, uf, cargo_codigo, totalizacao_final) "
            "VALUES (:c, :cod, :t, :uf, :cargo, :f)"
        ),
        {"c": CICLO, "cod": codigo, "t": turno, "uf": uf, "cargo": cargo, "f": final},
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
    """Projeto 10 acompanha governador SP (2o turno) e dep. federal SP (eleito).
    Projeto 20 acompanha dep. federal RJ (arquivo ainda aberto)."""
    s = make_session()
    add_projeto(s, 10, "ana@x.com")
    add_projeto(s, 20, "bia@x.com")
    add_candidacy(s, 1, 111, office_code=3, state="SP", name="GOV SP")
    add_candidacy(s, 2, 222, office_code=6, state="SP", name="DEP SP")
    add_candidacy(s, 3, 333, office_code=6, state="RJ", name="DEP RJ")
    follow(s, 10, 1)
    follow(s, 10, 2)
    follow(s, 20, 3)
    _arquivo(s, 3, "sp")
    _arquivo(s, 6, "sp")
    _arquivo(s, 6, "rj", final=False)
    _resultado(s, 1, "2º turno")
    _resultado(s, 2, "Eleito por média")
    liberar_no_plano(s, 1)  # projetos nascem no plano 1 (eleicao_schema)
    return s


def test_so_fica_pronto_quem_tem_todos_os_arquivos_encerrados() -> None:
    s = _cenario()
    prontos, aguardando = montar_avisos(s, ciclo=CICLO, turno=1)
    assert [a.projeto_id for a in prontos] == [10]
    assert aguardando == 1
    situacoes = {i["nome"]: i["situacao"] for i in prontos[0].itens}
    assert situacoes == {"GOV SP": "2º turno", "DEP SP": "Eleito por média"}


def test_candidato_ausente_de_arquivo_encerrado_nao_trava() -> None:
    s = _cenario()
    add_candidacy(s, 4, 444, office_code=6, state="SP", name="INDEFERIDO")
    follow(s, 10, 4)
    prontos, _ = montar_avisos(s, ciclo=CICLO, turno=1)
    itens = {i["nome"]: i["situacao"] for i in prontos[0].itens}
    assert itens["INDEFERIDO"] == NAO_CONSTA


def test_flag_off_nao_envia() -> None:
    s = _cenario()
    mailer = FakeMailer()
    stats = enviar(s, ciclo=CICLO, send=mailer)
    assert stats.flag == "off" and mailer.enviados == []
    assert s.execute(text("SELECT count(*) FROM election_result_notice")).scalar() == 0


def test_flag_admins_so_envia_para_admin() -> None:
    s = _cenario()
    set_flag(s, "admins")
    mailer = FakeMailer()
    stats = enviar(s, ciclo=CICLO, send=mailer, admins=frozenset({"bia@x.com"}))
    assert mailer.enviados == []
    assert stats.fora_do_recorte == 1

    stats = enviar(s, ciclo=CICLO, send=mailer, admins=frozenset({"ana@x.com"}))
    assert [to for to, _ in mailer.enviados] == ["ana@x.com"]


def test_flag_all_respeita_o_plano_igual_ao_modal() -> None:
    """Mesma regra do resolve_for da API: em `all`, quem decide e o plano."""
    s = _cenario()
    set_flag(s, "all")
    s.execute(text("UPDATE projetos SET tier_id = 2 WHERE id = 10"))  # plano sem a flag
    s.commit()
    mailer = FakeMailer()
    stats = enviar(s, ciclo=CICLO, send=mailer, admins=frozenset())
    assert mailer.enviados == [] and stats.fora_do_recorte == 1

    # admin recebe mesmo em plano sem a flag (previa e conferencia)
    enviar(s, ciclo=CICLO, send=mailer, admins=frozenset({"ana@x.com"}))
    assert [to for to, _ in mailer.enviados] == ["ana@x.com"]


def test_rodar_duas_vezes_envia_uma() -> None:
    s = _cenario()
    set_flag(s, "all")
    mailer = FakeMailer()
    enviar(s, ciclo=CICLO, send=mailer)
    stats = enviar(s, ciclo=CICLO, send=mailer)
    assert len(mailer.enviados) == 1
    assert stats.ja_enviados == 1
    to, subject = mailer.enviados[0]
    assert subject == "Saiu o resultado dos candidatos que você acompanha"

    row = s.execute(
        text("SELECT email_status, tentativas, payload, sent_at FROM election_result_notice")
    ).one()
    assert row.email_status == "sent" and row.tentativas == 1 and row.sent_at is not None
    payload = json.loads(row.payload)
    assert payload["turno"] == 1 and len(payload["itens"]) == 2
    log = s.execute(text("SELECT periodicidade, status FROM email_send_log")).all()
    assert [(r.periodicidade, r.status) for r in log] == [("eleicao_ele2026_t1", "sent")]


def test_falha_de_smtp_tenta_de_novo_ate_o_limite() -> None:
    s = _cenario()
    set_flag(s, "all")
    mailer = FakeMailer(falhar_para={"ana@x.com"})
    for _ in range(5):
        enviar(s, ciclo=CICLO, send=mailer)
    row = s.execute(text("SELECT email_status, tentativas, ultimo_erro FROM election_result_notice")).one()
    assert row.email_status == "error" and row.tentativas == 3
    assert "SMTP" in row.ultimo_erro

    mailer.falhar_para.clear()
    s.execute(text("UPDATE election_result_notice SET tentativas = 1"))
    s.commit()
    enviar(s, ciclo=CICLO, send=mailer)
    assert [to for to, _ in mailer.enviados] == ["ana@x.com"]


def test_segundo_turno_so_para_quem_acompanha_quem_foi_ao_segundo_turno() -> None:
    s = _cenario()
    set_flag(s, "all")
    add_projeto(s, 30, "caio@x.com")
    follow(s, 30, 2)  # so o deputado eleito: nao tem 2o turno
    mailer = FakeMailer()
    enviar(s, ciclo=CICLO, send=mailer)
    mailer.enviados.clear()

    _arquivo(s, 3, "sp", turno=2, codigo=6260)
    _resultado(s, 1, "Eleito", turno=2)
    enviar(s, ciclo=CICLO, send=mailer)
    assert mailer.enviados == [
        ("ana@x.com", "Saiu o resultado do 2º turno dos candidatos que você acompanha")
    ]
    payload = json.loads(
        s.execute(text("SELECT payload FROM election_result_notice WHERE turno = 2")).scalar()
    )
    assert [i["nome"] for i in payload["itens"]] == ["GOV SP"]
    assert payload["itens"][0]["situacao"] == "Eleito"


def test_segundo_turno_espera_o_arquivo_do_segundo_turno() -> None:
    s = _cenario()
    prontos, aguardando = montar_avisos(s, ciclo=CICLO, turno=2)
    assert prontos == [] and aguardando == 1


def test_projeto_excluido_nao_recebe() -> None:
    s = _cenario()
    set_flag(s, "all")
    s.execute(text("UPDATE projetos SET deleted_at = CURRENT_TIMESTAMP WHERE id = 10"))
    s.commit()
    mailer = FakeMailer()
    enviar(s, ciclo=CICLO, send=mailer)
    assert mailer.enviados == []


def test_html_lista_candidatos_com_votos() -> None:
    s = _cenario()
    prontos, _ = montar_avisos(s, ciclo=CICLO, turno=1)
    corpo = render_html(prontos[0])
    assert "GOV SP" in corpo and "2º turno" in corpo
    assert "1.000 votos (12,50%)" in corpo
    assert "{{" not in corpo
    assert "/candidaturas" in corpo
