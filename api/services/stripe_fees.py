"""Taxas da Stripe no mês corrente (CS-121), para o KPI da aba Financeiro.

Lê as transações de saldo da conta Stripe do Mamute ("Mamute Político",
conectada ao Ghost) com uma chave restrita somente leitura em
`STRIPE_MAMUTE_RESTRICTED_KEY`. O nome é explícito de propósito: a empresa
tem outra conta Stripe (Correio Sabiá) e a chave dela não serve aqui.

Sem a chave, devolve `disponivel = False` e o painel mostra o que falta.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

ENV_CHAVE = "STRIPE_MAMUTE_RESTRICTED_KEY"
_URL = "https://api.stripe.com/v1/balance_transactions"


def taxas_do_mes(
    *,
    agora: Optional[datetime] = None,
    chave: Optional[str] = None,
    http_get: Callable[..., Any] = requests.get,
) -> dict[str, Any]:
    chave = chave or os.getenv(ENV_CHAVE)
    if not chave:
        return {
            "disponivel": False,
            "motivo": "Falta a chave restrita da conta Stripe “Mamute Político” na API.",
        }
    agora = agora or datetime.now(timezone.utc)
    inicio = datetime(agora.year, agora.month, 1, tzinfo=timezone.utc)
    bruto = taxas = 0
    cobrancas = 0
    params: dict[str, Any] = {"limit": 100, "created[gte]": int(inicio.timestamp())}
    try:
        while True:
            r = http_get(_URL, params=params, auth=(chave, ""), timeout=20)
            r.raise_for_status()
            corpo = r.json()
            for t in corpo.get("data", []):
                if t.get("type") in ("charge", "payment"):
                    bruto += int(t.get("amount") or 0)
                    cobrancas += 1
                taxas += int(t.get("fee") or 0)
            if not corpo.get("has_more") or not corpo.get("data"):
                break
            params["starting_after"] = corpo["data"][-1]["id"]
    except requests.RequestException as exc:
        return {"disponivel": False, "motivo": f"Stripe não respondeu: {str(exc)[:200]}"}
    return {
        "disponivel": True,
        "recebido_bruto": round(bruto / 100, 2),
        "taxas": round(taxas / 100, 2),
        "taxas_percentual": round(100 * taxas / bruto, 1) if bruto else None,
        "cobrancas": cobrancas,
    }


__all__ = ["ENV_CHAVE", "taxas_do_mes"]
