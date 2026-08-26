#!/usr/bin/env python3
"""
INNVISTA — sincronização Stays → Postgres

Roda no GitHub Actions a cada 15 minutos. Substitui o sync_stays do
Apps Script, e com ele somem os limites que atrapalhavam lá: não há
teto de 6 minutos por execução nem cota de 90 minutos por dia.

Três passos independentes. Se um falhar, os outros continuam — na
planilha, um erro no meio deixava tudo pela metade sem avisar.

    catálogo       1×/dia    imóveis ativos da Stays
    reservas       sempre    janela curta, o que o time usa
    reconciliação  1×/hora   canceladas, alteradas e bloqueios

Uso:
    python sync_stays.py                 # ciclo normal
    python sync_stays.py --completo      # força catálogo + reconciliação
    python sync_stays.py --cadastro a.csv  # importa empresa/vaga/facial

Variáveis de ambiente (Secrets do GitHub):
    STAYS_DOMAIN            https://innvista.stays.net
    STAYS_CLIENT_ID
    STAYS_CLIENT_SECRET
    SUPABASE_URL            https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY    a chave service_role
"""

import os
import sys
import csv
import json
import base64
import argparse
from datetime import date, datetime, timedelta, timezone

import requests

# --------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------

TZ = timezone(timedelta(hours=-3))          # America/Sao_Paulo

# Janelas operacionais. Na planilha eram 365 dias, duas vezes por
# execução — aqui não há motivo para exagerar: o time trabalha o mês
# corrente, e o histórico já está no banco.
JANELA_ARRIVAL_ATRAS = 3
JANELA_ARRIVAL_FRENTE = 60
JANELA_DEPARTURE_ATRAS = 15
JANELA_DEPARTURE_FRENTE = 15

TEMPO_LIMITE = 60          # segundos por chamada HTTP
LOTE = 500                 # registros por gravação no Postgres


def env(nome: str) -> str:
    v = os.environ.get(nome, "").strip()
    if not v:
        sys.exit(f"Falta a variável de ambiente {nome}")
    return v


STAYS = env("STAYS_DOMAIN").rstrip("/")
STAYS_AUTH = "Basic " + base64.b64encode(
    f'{env("STAYS_CLIENT_ID")}:{env("STAYS_CLIENT_SECRET")}'.encode()
).decode()

SB = env("SUPABASE_URL").rstrip("/")
SB_KEY = env("SUPABASE_SERVICE_KEY")

# O Supabase tem dois formatos de chave, e eles NÃO usam os mesmos cabeçalhos.
#
#   sb_secret_...  (novo)     → só o cabeçalho apikey.
#                               Mandar em Authorization: Bearer devolve 401.
#   eyJhb...       (legado)   → apikey + Authorization: Bearer, como sempre.
#
# As legadas (anon / service_role) foram descontinuadas e param de funcionar
# no fim de 2026. Este código aceita as duas: detecta pelo prefixo.
SB_HEAD = {"apikey": SB_KEY, "Content-Type": "application/json"}
if not SB_KEY.startswith(("sb_secret_", "sb_publishable_")):
    SB_HEAD["Authorization"] = f"Bearer {SB_KEY}"


if SB_KEY.startswith("sb_publishable_"):
    sys.exit("SUPABASE_SERVICE_KEY recebeu a chave publishable (do painel). "
             "A sincronização precisa da chave secreta (sb_secret_... ou service_role).")


def hoje() -> date:
    return datetime.now(TZ).date()


def dia(offset: int) -> str:
    return (hoje() + timedelta(days=offset)).isoformat()


def log(msg: str) -> None:
    print(f"{datetime.now(TZ):%H:%M:%S}  {msg}", flush=True)


# --------------------------------------------------------------------
# Stays
# --------------------------------------------------------------------

def stays_export(de: str, ate: str, date_type: str) -> list:
    """
    POST /booking/reservations-export — devolve tudo de uma vez.

    Só aceita reserved, booked e contract. Um type=canceled aqui é
    ignorado em silêncio: canceladas vêm pelo stays_buscar.
    """
    r = requests.post(
        f"{STAYS}/external/v1/booking/reservations-export",
        headers={"Authorization": STAYS_AUTH, "Accept": "application/json"},
        json={"from": de, "to": ate, "dateType": date_type},
        timeout=TEMPO_LIMITE,
    )
    r.raise_for_status()
    dados = r.json()
    return dados if isinstance(dados, list) else []


def stays_buscar(de: str, ate: str, date_type: str, tipos: list) -> list:
    """
    GET /booking/reservations — paginado de verdade.

    Este endpoint devolve no máximo 20 registros por chamada. A versão
    da planilha não paginava: fatiava o período em duas janelas e
    torcia. Quando o intervalo tinha mais de 20 registros, os
    excedentes sumiam — foi assim que bloqueios de D+1 e D+2
    desapareciam das mensagens de limpeza.
    """
    saida, pulo, limite = [], 0, 20
    while True:
        params = [("from", de), ("to", ate), ("dateType", date_type),
                  ("limit", limite), ("skip", pulo)]
        params += [("type", t) for t in tipos]

        r = requests.get(
            f"{STAYS}/external/v1/booking/reservations",
            headers={"Authorization": STAYS_AUTH, "Accept": "application/json"},
            params=params, timeout=TEMPO_LIMITE,
        )
        r.raise_for_status()
        pagina = r.json()
        if not isinstance(pagina, list) or not pagina:
            break
        saida += pagina
        if len(pagina) < limite:
            break
        pulo += limite
        if pulo > 5000:                      # trava de segurança
            log("stays_buscar: passou de 5000 registros, parando")
            break
    return saida


def stays_listings() -> list:
    saida, pulo, limite = [], 0, 100
    while True:
        r = requests.get(
            f"{STAYS}/external/v1/content/listings",
            headers={"Authorization": STAYS_AUTH, "Accept": "application/json"},
            params={"status": "active", "limit": limite, "skip": pulo},
            timeout=TEMPO_LIMITE,
        )
        r.raise_for_status()
        pagina = r.json()
        if not isinstance(pagina, list) or not pagina:
            break
        saida += pagina
        if len(pagina) < limite:
            break
        pulo += limite
    return saida


# --------------------------------------------------------------------
# Supabase (PostgREST)
# --------------------------------------------------------------------

def gravar(tabela: str, linhas: list, conflito: str) -> int:
    """
    Upsert. Só as colunas presentes no payload são atualizadas — é o
    que preserva empresa_limpeza, tem_vaga e tem_facial em listings,
    que são mantidas por gente e não vêm da Stays.
    """
    if not linhas:
        return 0
    total = 0
    for i in range(0, len(linhas), LOTE):
        pedaco = linhas[i:i + LOTE]
        r = requests.post(
            f"{SB}/rest/v1/{tabela}",
            headers={**SB_HEAD,
                     "Prefer": f"resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": conflito},
            data=json.dumps(pedaco, default=str),
            timeout=TEMPO_LIMITE,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"{tabela}: HTTP {r.status_code} — {r.text[:400]}")
        total += len(pedaco)
    return total


def consultar(tabela: str, params: dict) -> list:
    r = requests.get(f"{SB}/rest/v1/{tabela}", headers=SB_HEAD,
                     params=params, timeout=TEMPO_LIMITE)
    r.raise_for_status()
    return r.json()


def atualizar(tabela: str, filtro: dict, valores: dict) -> None:
    r = requests.patch(f"{SB}/rest/v1/{tabela}", headers={**SB_HEAD, "Prefer": "return=minimal"},
                       params=filtro, data=json.dumps(valores, default=str),
                       timeout=TEMPO_LIMITE)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela} patch: HTTP {r.status_code} — {r.text[:400]}")


# --------------------------------------------------------------------
# Conversão
# --------------------------------------------------------------------

def normalizar_apto(nome: str) -> str:
    """Mesma normalização da planilha: 'Uwin 105 -- Units: 2' → 'Uwin 105'."""
    import re
    s = (nome or "").strip()
    for padrao in (r"^([A-Za-zÀ-ÿ]+)\s+([A-Za-zÀ-ÿ]+)\s+(\d{1,5})\b",
                   r"^([A-Za-zÀ-ÿ]+)\s+(\d{1,5})\b",
                   r"^([A-Za-zÀ-ÿ]+)(\d{1,5})$"):
        m = re.match(padrao, s)
        if m:
            return " ".join(m.groups())
    return " ".join(s.split()[:2])


def so_data(valor) -> str | None:
    return str(valor)[:10] if valor else None


def montar_reserva(res: dict, listing_id: str | None) -> dict:
    cliente = res.get("client") or {}
    detalhes = res.get("guestsDetails") or {}
    hospedes = (res.get("guests") or res.get("guestTotalCount")
                or sum(int(detalhes.get(k) or 0) for k in ("adults", "children", "infants"))
                or 1)
    return {
        "id": res.get("id") or res.get("_id"),
        "codigo_canal": res.get("partnerCode"),
        "listing_id": listing_id,
        "hospede_nome": (cliente.get("name") or "")[:120],
        # Mantida a correção da v2.8 da planilha: phoneNumber primeiro,
        # e nada de chamada individual por reserva — era o que estourava
        # o tempo lá.
        "hospede_fone": (cliente.get("phoneNumber") or "").replace(" ", "") or None,
        "check_in": so_data(res.get("checkInDate")),
        "check_out": so_data(res.get("checkOutDate")),
        "hospedes": int(hospedes),
        "canal": (res.get("partner") or {}).get("name") or res.get("agent") or None,
        "criada_em": res.get("creationDate") or res.get("createdAt"),
        "raw": res,
        "sync_em": datetime.now(TZ).isoformat(),
    }


# --------------------------------------------------------------------
# Passos
# --------------------------------------------------------------------

def passo_catalogo() -> dict:
    """Imóveis ativos. Não toca em empresa_limpeza, tem_vaga, tem_facial."""
    brutos = stays_listings()
    linhas, por_id = [], {}
    for l in brutos:
        ident = l.get("_id") or l.get("id")
        nome = normalizar_apto(l.get("internalName") or "")
        if not ident or not nome:
            continue
        linhas.append({"id": ident, "codigo": l.get("id"), "nome": nome,
                       "ativo": True, "atualizado_em": datetime.now(TZ).isoformat()})
        por_id[ident] = nome
        if l.get("id"):
            por_id[l["id"]] = nome
    gravar("listings", linhas, "id")
    log(f"catálogo: {len(linhas)} imóveis ativos")
    return por_id


def mapa_listings() -> dict:
    """id → nome, direto do banco (evita re-chamar a Stays a cada rodada)."""
    return {l["id"]: l["nome"] for l in consultar("listings", {"select": "id,nome"})}


def passo_reservas(validos: set) -> int:
    lotes = [
        stays_export(dia(-JANELA_ARRIVAL_ATRAS), dia(JANELA_ARRIVAL_FRENTE), "arrival"),
        stays_export(dia(-JANELA_DEPARTURE_ATRAS), dia(JANELA_DEPARTURE_FRENTE), "departure"),
    ]
    vistos, linhas = set(), []
    for lote in lotes:
        for res in lote:
            ident = res.get("id") or res.get("_id")
            if not ident or ident in vistos:
                continue
            vistos.add(ident)
            listing_id = res.get("_idlisting")
            if listing_id not in validos:
                continue                      # imóvel desativado: não gera trabalho
            r = montar_reserva(res, listing_id)
            if r["check_in"] and r["check_out"]:
                r["status"] = "ativa"
                linhas.append(r)
    gravar("reservations", linhas, "id")
    log(f"reservas: {len(linhas)} ativas gravadas")
    return len(linhas)


def passo_reconciliacao(validos: set) -> int:
    """
    Canceladas, alteradas e bloqueios.

    A parte que a planilha nunca fez direito: quando o canal cancela e
    recria uma reserva mantendo o mesmo código de confirmação, isso é
    uma ALTERAÇÃO, não uma duplicata. Lá ficavam duas linhas, uma morta
    e uma viva, e o time refazia tudo do zero.
    """
    canceladas = stays_buscar(dia(-JANELA_DEPARTURE_ATRAS), dia(JANELA_ARRIVAL_FRENTE),
                              "arrival", ["canceled"])

    ativas_por_codigo = {}
    for r in consultar("reservations", {"select": "id,codigo_canal",
                                        "status": "eq.ativa",
                                        "codigo_canal": "not.is.null"}):
        ativas_por_codigo[r["codigo_canal"]] = r["id"]

    linhas = []
    for res in canceladas:
        ident = res.get("id") or res.get("_id")
        listing_id = res.get("_idlisting")
        if not ident or listing_id not in validos:
            continue

        codigo = res.get("partnerCode")
        sucessora = ativas_por_codigo.get(codigo) if codigo else None
        alteracao = bool(sucessora and sucessora != ident)

        r = montar_reserva(res, listing_id)
        r["status"] = "alterada" if alteracao else "cancelada"
        r["cancelada_em"] = (res.get("canceledAt") or res.get("cancelledAt")
                             or datetime.now(TZ).isoformat())
        r["substituida_por"] = sucessora if alteracao else None
        if r["check_in"] and r["check_out"]:
            linhas.append(r)

    gravar("reservations", linhas, "id")

    # A herança das tarefas do time (documento conferido continua
    # conferido; senhas e facial voltam a pendente) acontece por gatilho
    # no banco, dentro da mesma transação. Ver herdar_tarefas() no
    # schema.sql. O worker não escreve em tasks — nem pode.
    alteradas = [l for l in linhas if l["status"] == "alterada"]

    # Bloqueios e manutenções.
    brutos = stays_buscar(dia(-JANELA_DEPARTURE_ATRAS), dia(365),
                          "departure", ["blocked", "maintenance"])
    bloqueios, vistos = [], set()
    for b in brutos:
        ident = b.get("id") or b.get("_id")
        listing_id = b.get("_idlisting")
        if not ident or ident in vistos or listing_id not in validos:
            continue
        vistos.add(ident)
        bloqueios.append({
            "id": ident, "listing_id": listing_id,
            "tipo": "manutencao" if b.get("type") == "maintenance" else "bloqueio",
            "inicio": so_data(b.get("checkInDate")),
            "fim": so_data(b.get("checkOutDate")),
            "sync_em": datetime.now(TZ).isoformat(),
        })
    gravar("blocks", [b for b in bloqueios if b["inicio"] and b["fim"]], "id")

    log(f"reconciliação: {len(linhas)} eventos ({len(alteradas)} alterações), "
        f"{len(bloqueios)} bloqueios")
    return len(linhas)


def passo_limpezas(validos: set) -> int:
    """Propõe as limpezas do dia. Nunca mexe no que o time já escreveu."""
    hoje_iso = hoje().isoformat()
    propostas = {}

    for r in consultar("reservations", {"select": "id,listing_id",
                                        "status": "eq.ativa",
                                        "check_out": f"eq.{hoje_iso}"}):
        propostas[r["listing_id"]] = ("check-out", r["id"])

    for b in consultar("blocks", {"select": "id,listing_id,tipo", "fim": f"eq.{hoje_iso}"}):
        propostas[b["listing_id"]] = (
            "manutencao" if b["tipo"] == "manutencao" else "bloqueio", b["id"])

    empresas = {l["id"]: l.get("empresa_limpeza")
                for l in consultar("listings", {"select": "id,empresa_limpeza"})}

    linhas = [{"dia": hoje_iso, "listing_id": lid, "empresa": empresas.get(lid),
               "origem": origem, "origem_id": oid, "status": "pendente"}
              for lid, (origem, oid) in propostas.items() if lid in validos]

    # ignoreDuplicates: se a limpeza do dia já existe, não sobrescreve o
    # status nem a ocorrência que o time anotou.
    if linhas:
        r = requests.post(
            f"{SB}/rest/v1/cleanings",
            headers={**SB_HEAD, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            params={"on_conflict": "dia,listing_id"},
            data=json.dumps(linhas, default=str), timeout=TEMPO_LIMITE)
        if r.status_code >= 300:
            raise RuntimeError(f"cleanings: HTTP {r.status_code} — {r.text[:400]}")

    de_bloqueio = sum(1 for l in linhas if l["origem"] != "check-out")
    log(f"limpezas de hoje: {len(linhas)} ({de_bloqueio} não vieram de check-out)")
    return len(linhas)


# --------------------------------------------------------------------
# Cadastro (importação única, a partir do cadastro_apartamentos)
# --------------------------------------------------------------------

def importar_cadastro(caminho: str) -> None:
    """
    Traz empresa de limpeza, vaga e facial do cadastro_apartamentos.

    Exporte a aba 'Auxiliar pós cadastro' como CSV e rode:
        python sync_stays.py --cadastro cadastro.csv

    Espera as colunas: apartamento, empresa, vaga, facial
    (renomeie no CSV antes, ou ajuste os nomes aqui).
    """
    por_nome = {l["nome"]: l["id"] for l in consultar("listings", {"select": "id,nome"})}
    linhas, ausentes = [], []

    with open(caminho, encoding="utf-8-sig", newline="") as f:
        for reg in csv.DictReader(f):
            nome = normalizar_apto(reg.get("apartamento", ""))
            ident = por_nome.get(nome)
            if not ident:
                ausentes.append(nome)
                continue
            vaga = (reg.get("vaga") or "").strip()
            linhas.append({
                "id": ident,
                "empresa_limpeza": (reg.get("empresa") or "").strip() or None,
                "tem_vaga": vaga.lower().startswith("sim"),
                "vaga_detalhe": vaga if vaga.lower().startswith("sim") else None,
                "tem_facial": (reg.get("facial") or "").strip().lower().startswith("sim"),
            })

    gravar("listings", linhas, "id")
    log(f"cadastro: {len(linhas)} imóveis atualizados")
    if ausentes:
        log(f"  não encontrados no catálogo da Stays ({len(ausentes)}): "
            + ", ".join(sorted(set(ausentes))[:15]))


# --------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--completo", action="store_true",
                    help="força catálogo e reconciliação")
    ap.add_argument("--cadastro", metavar="CSV",
                    help="importa empresa/vaga/facial e sai")
    args = ap.parse_args()

    if args.cadastro:
        importar_cadastro(args.cadastro)
        return 0

    agora = datetime.now(TZ)
    run = requests.post(f"{SB}/rest/v1/sync_runs", headers={**SB_HEAD, "Prefer": "return=representation"},
                        data=json.dumps({"inicio": agora.isoformat()}), timeout=TEMPO_LIMITE)
    run_id = run.json()[0]["id"] if run.status_code < 300 else None

    try:
        # Catálogo: 1×/dia, ou quando pedido.
        if args.completo or agora.hour == 4:
            passo_catalogo()
        validos = set(mapa_listings().keys())
        if not validos:
            passo_catalogo()
            validos = set(mapa_listings().keys())

        n_res = passo_reservas(validos)

        # Reconciliação: 1×/hora. É o passo que descobre cancelamento e
        # alteração — não precisa ser a cada 15 minutos, mas não pode
        # ficar mais de uma hora sem rodar.
        n_eventos = 0
        if args.completo or agora.minute < 15:
            n_eventos = passo_reconciliacao(validos)

        passo_limpezas(validos)

        if run_id:
            atualizar("sync_runs", {"id": f"eq.{run_id}"},
                      {"fim": datetime.now(TZ).isoformat(), "status": "ok",
                       "reservas": n_res, "bloqueios": n_eventos})
        log(f"✅ concluído em {(datetime.now(TZ) - agora).seconds}s")
        return 0

    except Exception as e:
        log(f"❌ {type(e).__name__}: {e}")
        if run_id:
            try:
                atualizar("sync_runs", {"id": f"eq.{run_id}"},
                          {"fim": datetime.now(TZ).isoformat(), "status": "erro",
                           "erro": str(e)[:1000]})
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
