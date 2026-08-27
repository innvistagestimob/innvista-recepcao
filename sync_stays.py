#!/usr/bin/env python3
"""
INNVISTA — sincronização Stays → Postgres

Roda no GitHub Actions a cada 15 minutos, das 09h à meia-noite de São
Paulo. Substitui o sync_stays do Apps Script, e com ele somem os limites
que atrapalhavam lá: não há teto de 6 minutos por execução nem cota de
90 minutos por dia.

A janela não é preguiça: no plano gratuito o Actions dá 2.000 minutos por
mês e cobra cada rodada como 1 minuto cheio. 24 horas de sincronização
custariam ~2.900 min/mês e a conta pararia no dia 20. Das 09h à meia-noite
são ~1.825 min/mês — cabe, e cobre o horário em que existe recepção.

Passos independentes. Se um falhar, os outros continuam — na planilha,
um erro no meio deixava tudo pela metade sem avisar.

    catálogo       1×/dia    imóveis ativos da Stays
    cadastro       sempre    empresa de limpeza, vaga e facial
    reservas       sempre    janela curta, o que o time usa
    reconciliação  1×/hora   canceladas, alteradas e bloqueios

O cadastro sai da tabela cadastros_apartamentos do próprio Supabase, que
você edita no editor de tabelas. Nada de reenviar arquivo: apartamento
novo é uma linha. O CSV continua aceito para uma carga avulsa.

Uso:
    python sync_stays.py                 # ciclo normal
    python sync_stays.py --completo      # força catálogo + reconciliação
    python sync_stays.py --cadastro a.csv  # carga avulsa por arquivo

Variáveis de ambiente (Secrets do GitHub):
    STAYS_DOMAIN            https://innvista.stays.net
    STAYS_CLIENT_ID
    STAYS_CLIENT_SECRET
    SUPABASE_URL            https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY    a chave service_role
"""

import os
import re
import sys
import csv
import json
import base64
import argparse
from datetime import date, datetime, timedelta, timezone

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------

# Carimbo impresso no começo de toda execução. Serve para responder, sem
# adivinhação, a pergunta que já custou caro: "é a versão nova que está
# rodando?". Se o log não mostrar esta linha, o arquivo no repositório é
# outro. Suba a versão sempre que mexer no arquivo.
VERSAO = "v4.1 (nome completo do apto, bloqueios que somem, cancelamento honesto)"

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
PAUSA_PAGINA = 0.15        # respiro entre páginas, para não irritar a API

# Tabela do Supabase com empresa de limpeza, vaga e facial. Mantida por
# você no editor de tabelas; lida a cada rodada. Se não existir, o passo
# sai em silêncio e nada quebra.
TABELA_CADASTRO = "cadastros_apartamentos"

# Hora (de São Paulo) em que o catálogo de imóveis é recarregado da Stays.
# Tem de ser uma hora em que o agendamento roda — ver o cron no sync.yml.
# Com a janela 09h–00h, 9 é a primeira rodada do dia.
HORA_CATALOGO = 9


def sessao() -> requests.Session:
    """
    Sessão com retentativa automática.

    "Connection reset by peer" é a API do outro lado derrubando a conexão —
    acontece em rajada de chamadas seguidas, e some sozinho quando se tenta
    de novo. Sem isto, uma queda de rede de um segundo derruba a
    sincronização inteira e o time fica com dado velho até a próxima rodada.

    São 4 tentativas com espera crescente (0,5s, 1s, 2s, 4s), cobrindo
    tanto erro de conexão quanto 429 e 5xx.
    """
    s = requests.Session()
    politica = Retry(
        total=4, connect=4, read=4, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PATCH", "DELETE"]),
        raise_on_status=False,
    )
    adaptador = HTTPAdapter(max_retries=politica, pool_connections=4, pool_maxsize=8)
    s.mount("https://", adaptador)
    s.mount("http://", adaptador)
    return s


HTTP = sessao()


def env(nome: str) -> str:
    v = os.environ.get(nome, "").strip()
    if not v:
        sys.exit(f"Falta a variável de ambiente {nome}")
    return v


STAYS = env("STAYS_DOMAIN").rstrip("/")
STAYS_AUTH = "Basic " + base64.b64encode(
    f'{env("STAYS_CLIENT_ID")}:{env("STAYS_CLIENT_SECRET")}'.encode()
).decode()

def url_supabase(bruta: str) -> str:
    """
    Normaliza a URL do projeto.

    A pegadinha: o painel do Supabase mostra a URL do projeto em um lugar
    e trechos de código com a URL da API em outro. Quem copia do trecho de
    código leva junto o /rest/v1 — e aí o caminho fica duplicado
    (/rest/v1/rest/v1/listings), o que devolve PGRST125,
    "Invalid path specified in request URL". O erro não fala em URL, fala
    em caminho, então custa a fazer sentido.
    """
    u = bruta.strip().rstrip("/")
    if not u.startswith(("http://", "https://")):
        u = "https://" + u

    for sufixo in ("/rest/v1", "/rest", "/auth/v1", "/storage/v1", "/graphql/v1"):
        if u.endswith(sufixo):
            u = u[: -len(sufixo)].rstrip("/")

    resto = u.split("://", 1)[1]
    if "/" in resto:
        sys.exit(f"SUPABASE_URL tem caminho a mais: {bruta}\n"
                 f"Use só o endereço do projeto, algo como "
                 f"https://{resto.split('/')[0]}")
    return u


SB = url_supabase(env("SUPABASE_URL"))
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
    r = HTTP.post(
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

        r = HTTP.get(
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
        time.sleep(PAUSA_PAGINA)
        if pulo > 5000:                      # trava de segurança
            log("stays_buscar: passou de 5000 registros, parando")
            break
    return saida


def stays_notas(de: str, ate: str) -> dict:
    """
    Nota interna ("observações" da Stays) das reservas que saem na janela.

    Por que existe uma função só para isto: os dois endpoints devolvem
    campos diferentes, e o campo que a recepção usa para escrever
    "Sem limpeza" está só num deles.

        POST /booking/reservations-export   35 campos, SEM internalNote
        GET  /booking/reservations          27 campos, COM internalNote

    Como as reservas ativas vêm do export, a observação nunca chegava. Aqui
    a gente busca de novo, pelo paginado, só a janela de saídas que
    interessa para a limpeza — algo como 60 registros, 3 páginas.

    Devolve {id_da_reserva: nota}. Falha em silêncio de propósito: se a
    Stays recusar o filtro por tipo, perder a observação não pode derrubar
    a sincronização inteira. O log diz quantas vieram.
    """
    notas = {}
    try:
        brutas = stays_buscar(de, ate, "departure", ["reserved", "booked", "contract"])
    except Exception as e:
        log(f"notas internas: falhou ({type(e).__name__}) — seguindo sem elas")
        return notas
    for r in brutas:
        ident = r.get("id") or r.get("_id")
        nota = (r.get("internalNote") or "").strip()
        if ident and nota:
            notas[ident] = nota[:2000]
    log(f"notas internas: {len(notas)} de {len(brutas)} reservas na janela de saída")
    return notas


def stays_listings() -> list:
    saida, pulo, limite = [], 0, 100
    while True:
        r = HTTP.get(
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
        time.sleep(PAUSA_PAGINA)
    return saida


# --------------------------------------------------------------------
# Supabase (PostgREST)
# --------------------------------------------------------------------

def gravar(tabela: str, linhas: list, conflito: str) -> int:
    """
    Upsert. Só as colunas presentes no payload são atualizadas — é o
    que preserva empresa_limpeza, tem_vaga e tem_facial em listings,
    que são mantidas por gente e não vêm da Stays.

    Cuidado ao montar o payload: isto vira INSERT ... ON CONFLICT DO
    UPDATE. O Postgres valida as restrições do INSERT mesmo quando a
    linha já existe e só o UPDATE vai acontecer — então toda coluna
    not null precisa vir junto, ainda que você não queira mudá-la.
    """
    if not linhas:
        return 0
    total = 0
    for i in range(0, len(linhas), LOTE):
        pedaco = linhas[i:i + LOTE]
        r = HTTP.post(
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
    r = HTTP.get(f"{SB}/rest/v1/{tabela}", headers=SB_HEAD,
                     params=params, timeout=TEMPO_LIMITE)
    r.raise_for_status()
    return r.json()


def atualizar(tabela: str, filtro: dict, valores: dict) -> None:
    r = HTTP.patch(f"{SB}/rest/v1/{tabela}", headers={**SB_HEAD, "Prefer": "return=minimal"},
                       params=filtro, data=json.dumps(valores, default=str),
                       timeout=TEMPO_LIMITE)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela} patch: HTTP {r.status_code} — {r.text[:400]}")


# --------------------------------------------------------------------
# Conversão
# --------------------------------------------------------------------

def normalizar_apto(nome: str) -> str:
    """
    'Uwin 105 -- Units: 2' → 'Uwin 105'. 'Movi Campo Belo 1802' inteiro.

    A versão anterior era herança da planilha e tinha um teto embutido: os
    padrões cobriam nomes de UMA ou DUAS palavras antes do número, e o que
    não casasse caía num `" ".join(s.split()[:2])` que simplesmente cortava
    no segundo termo. "Movi Campo Belo 1802" virava "Movi Campo" — e como
    esse é o nome que vai para o catálogo, o apartamento aparecia truncado
    em toda parte: no painel, na mensagem da empresa e no casamento com o
    cadastro. Um corte silencioso, que só se percebe olhando um nome longo.

    A regra agora não tem teto: o nome é tudo até o primeiro número,
    incluindo ele. Qualquer coisa depois — "-- Units: 2", "(Studio)" — é
    sufixo da Stays e sai fora.
    """
    s = (nome or "").strip()
    if not s:
        return ""
    # "Uwin105" e "Uwin 105" precisam virar a mesma coisa, senão o índice de
    # nomes guarda duas chaves diferentes para o mesmo apartamento.
    s = re.sub(r"([A-Za-zÀ-ÿ])(\d)", r"\1 \2", s)
    m = re.search(r"\d{1,5}", s)
    if m:
        return " ".join(s[:m.end()].split())
    return " ".join(s.split())


def so_data(valor) -> str | None:
    return str(valor)[:10] if valor else None


def chave_nome(s: str) -> str:
    """
    Reduz um nome de imóvel à sua forma comparável.

    Existe porque o mesmo apartamento aparece escrito de jeitos diferentes
    em cada lugar: "Brera 127", "BRERA127", "Brera  127 -- Units: 2",
    "Brera 127 (Studio)". Sem acento, sem maiúscula, sem pontuação e sem
    espaço sobrando, os quatro viram "brera 127" e casam.
    """
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def indice_de_nomes(pares) -> dict:
    """
    Índice nome → id, com várias grafias apontando para o mesmo imóvel.

    Para cada imóvel entram duas chaves: o nome como está no catálogo e a
    forma curta ("Brera 127 -- Units: 2" → "brera 127"). Colisão não
    sobrescreve: o primeiro a chegar fica, para um apelido ambíguo não
    roubar o imóvel de outro.
    """
    indice = {}
    for nome, ident in pares:
        for variante in (nome, normalizar_apto(nome)):
            k = chave_nome(variante)
            if k and k not in indice:
                indice[k] = ident
    return indice


def id_do_imovel(res: dict, por_nome: dict, validos: set | None = None) -> str | None:
    """
    Descobre a qual imóvel a reserva pertence.

    Aqui mora o bug do "reservas: 0". Os endpoints da Stays devolvem o
    imóvel de formas diferentes, e a v3.2 ainda errou porque *adivinhava*
    o nome do campo em vez de conferir o valor contra o catálogo:

        GET  /booking/reservations         → res["_idlisting"]  (funciona)
        POST /booking/reservations-export  → ?                  (não era isso)

    A v3.2 devolvia o primeiro campo que existisse, mesmo que o valor não
    fosse um id do catálogo. Quem chamava então descartava a reserva — e o
    log dizia "sem imóvel no catálogo", quando o certo seria "achei um
    campo, mas o valor não serve".

    A v3.3 inverte a lógica: **só devolve um valor que esteja no
    catálogo.** Se o campo esperado não serve, ela continua procurando —
    inclusive varrendo o resto do dicionário. Assim, se a Stays mudar o
    nome do campo de novo, o casamento continua acontecendo sozinho.

    `validos` é o conjunto de ids do catálogo. Sem ele (chamada antiga),
    a função volta a se comportar como antes.
    """
    def serve(v):
        return isinstance(v, str) and v and (validos is None or v in validos)

    def por_apelido(v):
        """Tenta o nome como veio e também a forma curta ('Brera 127 -- Units: 2')."""
        if not isinstance(v, str) or not v:
            return None
        for variante in (v, normalizar_apto(v)):
            achado = por_nome.get(chave_nome(variante))
            if achado:
                return achado
        return None

    # 1. Campos diretos, nas grafias que a Stays já usou.
    for campo in ("_idlisting", "idListing", "_idListing", "listingId", "idlisting"):
        if serve(res.get(campo)):
            return res[campo]

    # 2. O campo "listing" — id solto, objeto, ou (o caso daqui) o NOME.
    listing = res.get("listing")
    if serve(listing):
        return listing
    if isinstance(listing, str):
        achado = por_apelido(listing)
        if achado:
            return achado
    if isinstance(listing, dict):
        for campo in ("_id", "id", "_idlisting", "listingId"):
            if serve(listing.get(campo)):
                return listing[campo]
        for campo in ("internalName", "name", "title", "id"):
            achado = por_apelido(listing.get(campo))
            if achado:
                return achado

    # 3. Nome do imóvel solto na raiz.
    for campo in ("listingInternalName", "internalName", "listingName",
                  "listingTitle", "apartment", "unit", "property"):
        achado = por_apelido(res.get(campo))
        if achado:
            return achado

    # 4. Rede final: qualquer valor, em qualquer campo, que seja um id do
    #    catálogo ou um nome conhecido. Formato-independente por construção —
    #    é o que impede este mesmo defeito de voltar com outro nome de campo.
    for v in res.values():
        if serve(v):
            return v
        achado = por_apelido(v)
        if achado:
            return achado
    for v in res.values():
        if isinstance(v, dict):
            for w in v.values():
                if serve(w):
                    return w
                achado = por_apelido(w)
                if achado:
                    return achado

    return None


def amostra_para_log(res: dict) -> str:
    """
    Retrato de uma reserva que não casou, sem vazar dado de hóspede.

    Mostra só os NOMES dos campos e os valores curtos que parecem id ou
    data. É o que permite descobrir o formato novo sem precisar de mais
    uma rodada de tentativa e erro.
    """
    partes = []
    for k, v in sorted(res.items()):
        if isinstance(v, dict):
            partes.append(f"{k}{{{','.join(sorted(v.keys())[:8])}}}")
        elif isinstance(v, list):
            partes.append(f"{k}[{len(v)}]")
        elif isinstance(v, str) and len(v) <= 40 and (
                "listing" in k.lower() or "date" in k.lower()
                or k.lower().endswith("id") or k.lower().startswith("_id")):
            partes.append(f"{k}={v}")
        else:
            partes.append(k)
    return " ".join(partes)[:900]


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
        "check_in": so_data(res.get("checkInDate") or res.get("arrivalDate")
                            or res.get("checkIn") or res.get("from")),
        "check_out": so_data(res.get("checkOutDate") or res.get("departureDate")
                             or res.get("checkOut") or res.get("to")),
        "hospedes": int(hospedes),
        # partnerName é o que o export manda (texto). partner.name só existe
        # no endpoint paginado. Procurar só o segundo deixava a coluna nula
        # em TODAS as reservas ativas — o painel nunca mostrou o canal.
        "canal": (res.get("partnerName")
                  or (res.get("partner") or {}).get("name")
                  or res.get("agent") or None),
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


def mapa_listings() -> tuple:
    """
    Do banco, nos dois sentidos: id → nome e apelido → id.

    O segundo mapa não é mais "nome exato → id": é o índice de apelidos,
    porque a Stays identifica o imóvel da reserva pelo NOME, não pelo id
    do catálogo. Ver id_do_imovel().
    """
    linhas = consultar("listings", {"select": "id,nome"})
    return ({l["id"]: l["nome"] for l in linhas},
            indice_de_nomes((l["nome"], l["id"]) for l in linhas))


def marcar_alteracoes(linhas: list, notas: dict) -> int:
    """
    Descobre o que mudou comparando com o que já está gravado.

    O detector anterior casava uma reserva cancelada com uma nova pelo
    partnerCode, partindo da ideia de que o canal cancela e recria. Só que
    as reservas diretas da Innvista têm partnerCode NULO — e a Stays altera
    a própria reserva, mantendo o mesmo id e trocando as datas. Resultado:
    a aba de alterações vivia zerada, e não por falta de alterações.

    Aqui a comparação é direta: datas, apartamento e número de hóspedes
    contra a linha que está no banco. Se mudou, é alteração.

    Duas sutilezas que custam caro se esquecidas:

    1. Toda linha leva as MESMAS chaves. O upsert do PostgREST monta as
       colunas a partir do primeiro objeto do lote — um objeto com chave a
       mais no meio da lista é ignorado em silêncio.
    2. alterada_em antigo é carregado adiante, não apagado. Quem limita a
       validade é a view, não a gravação.
    """
    if not linhas:
        return 0

    guardadas = {}
    ids = [l["id"] for l in linhas]
    for i in range(0, len(ids), 200):                # a URL tem limite
        pedaco = ",".join(f'"{x}"' for x in ids[i:i + 200])
        for r in consultar("reservations", {
                "select": "id,listing_id,check_in,check_out,hospedes,"
                          "alterada_em,alteracao_detalhe",
                "id": f"in.({pedaco})"}):
            guardadas[r["id"]] = r

    agora = datetime.now(TZ).isoformat()
    mudou_agora = 0

    for l in linhas:
        antes = guardadas.get(l["id"])
        l["nota_interna"] = notas.get(l["id"])
        l["alterada_em"] = antes.get("alterada_em") if antes else None
        l["alteracao_detalhe"] = antes.get("alteracao_detalhe") if antes else None
        if not antes:
            continue                                  # reserva nova não é alteração

        diffs = []
        if str(antes.get("check_in")) != str(l["check_in"]):
            diffs.append(f"check-in {br(antes.get('check_in'))} → {br(l['check_in'])}")
        if str(antes.get("check_out")) != str(l["check_out"]):
            diffs.append(f"check-out {br(antes.get('check_out'))} → {br(l['check_out'])}")
        if antes.get("listing_id") != l["listing_id"]:
            diffs.append("trocou de apartamento")
        if (antes.get("hospedes") or 0) != (l.get("hospedes") or 0):
            diffs.append(f"hóspedes {antes.get('hospedes')} → {l.get('hospedes')}")

        if diffs:
            l["alterada_em"] = agora
            l["alteracao_detalhe"] = "; ".join(diffs)[:500]
            mudou_agora += 1

    return mudou_agora


def br(d) -> str:
    """Data no formato que a recepção lê: 27/08."""
    t = str(d or "")[:10].split("-")
    return f"{t[2]}/{t[1]}" if len(t) == 3 else str(d)


def passo_reservas(validos: set, por_nome: dict) -> int:
    lotes = [
        stays_export(dia(-JANELA_ARRIVAL_ATRAS), dia(JANELA_ARRIVAL_FRENTE), "arrival"),
        stays_export(dia(-JANELA_DEPARTURE_ATRAS), dia(JANELA_DEPARTURE_FRENTE), "departure"),
    ]
    brutas = sum(len(l) for l in lotes)

    vistos, linhas = set(), []
    sem_imovel, sem_data, amostras = 0, 0, []
    for lote in lotes:
        for res in lote:
            ident = res.get("id") or res.get("_id")
            if not ident or ident in vistos:
                continue
            vistos.add(ident)

            listing_id = id_do_imovel(res, por_nome, validos)
            if listing_id not in validos:
                sem_imovel += 1               # imóvel desativado, ou não casou
                if len(amostras) < 2:         # retrato para diagnóstico
                    amostras.append(amostra_para_log(res))
                continue
            r = montar_reserva(res, listing_id)
            if not (r["check_in"] and r["check_out"]):
                sem_data += 1
                if len(amostras) < 2:
                    amostras.append(amostra_para_log(res))
                continue
            r["status"] = "ativa"
            linhas.append(r)

    notas = stays_notas(dia(-JANELA_DEPARTURE_ATRAS), dia(JANELA_DEPARTURE_FRENTE))
    alteradas = marcar_alteracoes(linhas, notas)

    gravar("reservations", linhas, "id")
    log(f"reservas: {len(linhas)} ativas gravadas "
        f"(de {brutas} recebidas, {len(vistos)} únicas, "
        f"{sem_imovel} sem imóvel no catálogo, {sem_data} sem data)")
    if alteradas:
        log(f"  alterações detectadas nesta rodada: {alteradas}")

    # Descartar quase tudo é sinal de que o casamento quebrou, não de que
    # o dia foi fraco. Antes de gritar, mostrar o formato do que veio —
    # sem isso, cada correção vira outra rodada de adivinhação.
    if brutas and not linhas:
        for i, a in enumerate(amostras, 1):
            log(f"amostra {i} do que não casou: {a}")
        raise RuntimeError(
            f"A Stays devolveu {brutas} reservas e nenhuma foi gravada "
            f"({len(validos)} imóveis no catálogo). Veja as amostras acima: "
            f"elas mostram os campos que vieram.")

    return len(linhas)


def passo_reconciliacao(validos: set, por_nome: dict) -> int:
    """
    Canceladas, alteradas e bloqueios.

    Sobre a data do cancelamento, que custou uma aba inteira errada:

    A API da Stays NÃO devolve quando a reserva foi cancelada. Conferi o
    payload de uma cancelada real (NM09J): tem creationDate, cancelMessage
    ("canceled on airbnb side"), stats — e nenhum campo com a data do
    cancelamento. A primeira versão preenchia com datetime.now(), e como a
    gravação é upsert, TODA rodada reescrevia o carimbo. Resultado: toda
    reserva cancelada da história parecia ter sido cancelada agora, e a aba
    do plantão enchia de cancelamento de julho.

    A correção tem duas partes:

    1. cancelada_em passa a ser "quando NÓS vimos o cancelamento", gravado
       uma vez só e nunca reescrito.
    2. era_ativa responde a pergunta que realmente importa: essa reserva
       chegou a existir como ativa no nosso banco? Se sim, o time pode ter
       preparado alguma coisa e o cancelamento é assunto do plantão. Se ela
       já nasceu cancelada aqui, é histórico anterior ao sistema — some.
    """
    canceladas = stays_buscar(dia(-JANELA_DEPARTURE_ATRAS), dia(JANELA_ARRIVAL_FRENTE),
                              "arrival", ["canceled"])

    ativas_por_codigo, guardadas = {}, {}
    for r in consultar("reservations", {"select": "id,codigo_canal,status,cancelada_em,era_ativa"}):
        guardadas[r["id"]] = r
        if r.get("status") == "ativa" and r.get("codigo_canal"):
            ativas_por_codigo[r["codigo_canal"]] = r["id"]

    agora = datetime.now(TZ).isoformat()
    linhas, novos_cancelamentos = [], 0
    for res in canceladas:
        ident = res.get("id") or res.get("_id")
        listing_id = id_do_imovel(res, por_nome, validos)
        if not ident or listing_id not in validos:
            continue

        codigo = res.get("partnerCode")
        sucessora = ativas_por_codigo.get(codigo) if codigo else None
        alteracao = bool(sucessora and sucessora != ident)

        antes = guardadas.get(ident)
        se_ja_cancelada = bool(antes and antes.get("status") in ("cancelada", "alterada"))

        r = montar_reserva(res, listing_id)
        r["status"] = "alterada" if alteracao else "cancelada"
        r["substituida_por"] = sucessora if alteracao else None
        r["nota_interna"] = (res.get("internalNote") or "").strip()[:2000] or None
        r["alterada_em"] = antes.get("alterada_em") if antes else None
        r["alteracao_detalhe"] = antes.get("alteracao_detalhe") if antes else None

        if se_ja_cancelada:
            # Já sabíamos. Preserva o carimbo original: reescrever faria a
            # reserva voltar ao topo da fila a cada 15 minutos, para sempre.
            r["cancelada_em"] = antes.get("cancelada_em") or agora
            r["era_ativa"] = bool(antes.get("era_ativa"))
        else:
            r["cancelada_em"] = agora
            # Estava ativa no nosso banco até agora → o time pode ter
            # preparado o apartamento. É isto que faz o cancelamento ser
            # assunto do plantão, e não uma linha de histórico.
            r["era_ativa"] = bool(antes and antes.get("status") == "ativa")
            if r["era_ativa"]:
                novos_cancelamentos += 1

        if r["check_in"] and r["check_out"]:
            linhas.append(r)

    gravar("reservations", linhas, "id")

    # A herança das tarefas do time (documento conferido continua
    # conferido; senhas e facial voltam a pendente) acontece por gatilho
    # no banco, dentro da mesma transação. Ver herdar_tarefas() no
    # schema.sql. O worker não escreve em tasks — nem pode.
    alteradas = [l for l in linhas if l["status"] == "alterada"]

    n_bloqueios = passo_bloqueios(validos, por_nome)

    log(f"reconciliação: {len(linhas)} eventos ({len(alteradas)} alterações), "
        f"{n_bloqueios} bloqueios")
    if novos_cancelamentos:
        log(f"  {novos_cancelamentos} cancelamento(s) de reserva que estava ativa — vão para o plantão")
    return len(linhas)


def passo_bloqueios(validos: set, por_nome: dict) -> int:
    """
    Bloqueios e manutenções — e a remoção dos que deixaram de existir.

    O que faltava: quando um bloqueio é cancelado ou tem a data alterada na
    Stays, ele simplesmente para de vir na resposta. Como a gravação é
    upsert, a linha antiga ficava no banco para sempre — e com ela a limpeza
    que ela tinha gerado, que continuava saindo na mensagem da empresa.
    Aconteceu com o Uwin 1514 (alterado) e o Uwin 2205 (cancelado).

    Agora o que sumiu da API some do banco. A trava é só uma: se a API
    devolver lista vazia, não apaga nada — lista vazia é bem mais provável
    ser falha de rede do que 100% dos bloqueios terem sido cancelados.
    """
    de, ate = dia(-JANELA_DEPARTURE_ATRAS), dia(365)
    brutos = stays_buscar(de, ate, "departure", ["blocked", "maintenance"])

    bloqueios, vistos = [], set()
    for b in brutos:
        ident = b.get("id") or b.get("_id")
        listing_id = id_do_imovel(b, por_nome, validos)
        if not ident or ident in vistos or listing_id not in validos:
            continue
        vistos.add(ident)
        bloqueios.append({
            "id": ident, "listing_id": listing_id,
            "tipo": "manutencao" if b.get("type") == "maintenance" else "bloqueio",
            "inicio": so_data(b.get("checkInDate")),
            "fim": so_data(b.get("checkOutDate")),
            "nota": descricao_bloqueio(b),
            "raw": b,
            "sync_em": datetime.now(TZ).isoformat(),
        })

    validos_bloqueios = [b for b in bloqueios if b["inicio"] and b["fim"]]
    gravar("blocks", validos_bloqueios, "id")

    if brutos:
        sumidos = [g["id"] for g in consultar("blocks", {
            "select": "id", "and": f"(fim.gte.{de},fim.lte.{ate})"})
            if g["id"] not in vistos]
        if sumidos:
            apagar_em_lotes("blocks", sumidos)
            log(f"  {len(sumidos)} bloqueio(s) sumiram da Stays e foram removidos")
    else:
        log("  a Stays não devolveu bloqueio nenhum — nada foi removido, por segurança")

    com_nota = sum(1 for b in validos_bloqueios if b["nota"])
    log(f"bloqueios: {len(validos_bloqueios)} ({com_nota} com descrição)")
    return len(validos_bloqueios)


def descricao_bloqueio(b: dict) -> str | None:
    """
    A descrição que a recepção escreve na Stays ao criar o bloqueio.

    O nome do campo não está documentado, então tenta os candidatos em
    ordem. O `raw` fica guardado no banco justamente para descobrir o nome
    certo sem precisar de outra rodada de tentativa e erro.
    """
    for campo in ("internalNote", "description", "note", "notes",
                  "cancelMessage", "title", "reason"):
        v = b.get(campo)
        if isinstance(v, str) and v.strip():
            return v.strip()[:2000]
    return None


def apagar_em_lotes(tabela: str, ids: list) -> None:
    for i in range(0, len(ids), 100):
        pedaco = ",".join(f'"{x}"' for x in ids[i:i + 100])
        r = HTTP.delete(f"{SB}/rest/v1/{tabela}", headers=SB_HEAD,
                        params={"id": f"in.({pedaco})"}, timeout=TEMPO_LIMITE)
        if r.status_code >= 300:
            raise RuntimeError(f"{tabela}: HTTP {r.status_code} ao apagar — {r.text[:300]}")


def passo_limpezas(validos: set) -> int:
    """
    Propõe as limpezas de hoje, amanhã e depois de amanhã.

    Três dias porque é isso que a empresa de limpeza precisa receber na
    véspera. Antes só existia o dia corrente, e o planejamento do dia
    seguinte era montado na mão, no WhatsApp.

    Duas regras que evitam mandar limpeza errada para a empresa:

    "Sem limpeza" na observação da Stays  → a limpeza não é criada.
        É decisão humana explícita. Foi escrita ali justamente para isso.

    Hóspede permanece (reservas emendadas) → a limpeza é criada com o
        alerta "Verificar".
        É dedução nossa, não declaração de ninguém. O caso real: Bruno
        Daniel R. no On Florida 1118, com três reservas seguidas, 17→27,
        27→28 e 28→29. Não há saída de verdade nos dias 27 e 28 — mas
        cancelar sozinho uma limpeza por dedução é arriscado demais.
        Melhor a empresa receber "Verificar" e confirmar.

    Nunca mexe no que o time já escreveu: a gravação é ignore-duplicates.
    """
    dias = [dia(0), dia(1), dia(2)]
    d_ini, d_fim = dias[0], dias[2]

    saidas = consultar("reservations", {
        "select": "id,listing_id,check_in,check_out,hospede_nome,nota_interna",
        "status": "eq.ativa",
        "and": f"(check_out.gte.{d_ini},check_out.lte.{d_fim})"})

    # As entradas servem para descobrir se o mesmo hóspede continua: a
    # reserva seguinte começa no dia em que esta termina.
    entradas = consultar("reservations", {
        "select": "id,listing_id,check_in,hospede_nome",
        "status": "eq.ativa",
        "and": f"(check_in.gte.{d_ini},check_in.lte.{d_fim})"})

    por_entrada = {}
    for e in entradas:
        por_entrada.setdefault((e["listing_id"], e["check_in"]), []).append(
            chave_nome(e.get("hospede_nome") or ""))

    bloqueios = consultar("blocks", {
        "select": "id,listing_id,tipo,fim",
        "and": f"(fim.gte.{d_ini},fim.lte.{d_fim})"})

    empresas = {l["id"]: l.get("empresa_limpeza")
                for l in consultar("listings", {"select": "id,empresa_limpeza"})}

    propostas, sem_limpeza, a_verificar = {}, 0, 0

    for r in saidas:
        lid, quando = r["listing_id"], r["check_out"]
        if lid not in validos:
            continue

        nota = (r.get("nota_interna") or "")
        if re.search(r"sem\s*limpeza", nota, re.I):
            sem_limpeza += 1
            continue

        eu = chave_nome(r.get("hospede_nome") or "")
        permanece = bool(eu) and eu in por_entrada.get((lid, quando), [])
        alerta = "Verificar" if permanece else None
        if permanece:
            a_verificar += 1

        propostas[(quando, lid)] = ("check-out", r["id"], alerta)

    # Bloqueio e manutenção têm precedência: verificação diferente, e é o
    # caso que a planilha escondia dentro de "check-out".
    for b in bloqueios:
        lid, quando = b["listing_id"], b["fim"]
        if lid not in validos:
            continue
        origem = "manutencao" if b["tipo"] == "manutencao" else "bloqueio"
        propostas[(quando, lid)] = (origem, b["id"], None)

    linhas = [{"dia": quando, "listing_id": lid, "empresa": empresas.get(lid),
               "origem": origem, "origem_id": oid, "status": "pendente",
               "alerta": alerta}
              for (quando, lid), (origem, oid, alerta) in propostas.items()]

    if linhas:
        r = HTTP.post(
            f"{SB}/rest/v1/cleanings",
            headers={**SB_HEAD, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            params={"on_conflict": "dia,listing_id,origem"},
            data=json.dumps(linhas, default=str), timeout=TEMPO_LIMITE)
        if r.status_code >= 300:
            raise RuntimeError(f"cleanings: HTTP {r.status_code} — {r.text[:400]}")

    for d in dias:
        doDia = [l for l in linhas if l["dia"] == d]
        bloq = sum(1 for l in doDia if l["origem"] != "check-out")
        log(f"limpezas {br(d)}: {len(doDia)} ({bloq} de bloqueio)")
    if sem_limpeza or a_verificar:
        log(f"  {sem_limpeza} puladas por \"sem limpeza\", "
            f"{a_verificar} marcadas para verificar (hóspede permanece)")
    return len(linhas)


# --------------------------------------------------------------------
# Cadastro (importação única, a partir do cadastro_apartamentos)
# --------------------------------------------------------------------

def coluna(colunas, *pistas) -> str | None:
    """Acha a coluna pelo que ela significa, não pelo nome exato."""
    for pista in pistas:
        for c in colunas:
            if pista in chave_nome(c).replace(" ", ""):
                return c
    return None


def aplicar_cadastro(registros: list, origem: str) -> int:
    """
    Escreve empresa de limpeza, vaga e facial nos imóveis.

    Recebe os registros já lidos — de um CSV ou da tabela do Supabase —
    e cuida do que é comum aos dois: achar as colunas, casar o imóvel
    pelo nome e gravar. Um único lugar para essa regra.
    """
    if not registros:
        log(f"cadastro ({origem}): nada a importar")
        return 0

    cols = list(registros[0].keys())
    c_apto = coluna(cols, "apartamento", "apto", "imovel", "unidade", "nome")
    c_empresa = coluna(cols, "empresa", "limpeza")
    c_vaga = coluna(cols, "vaga", "garagem")
    c_facial = coluna(cols, "facial", "biometri")
    if not c_apto:
        log(f"cadastro ({origem}): não achei a coluna do apartamento. "
            f"Colunas: {', '.join(map(str, cols))}")
        return 0
    log(f"cadastro ({origem}): apto={c_apto} empresa={c_empresa} "
        f"vaga={c_vaga} facial={c_facial}")

    catalogo = consultar("listings", {"select": "id,nome"})
    por_nome = indice_de_nomes((l["nome"], l["id"]) for l in catalogo)
    # O nome vai junto na gravação de propósito. O upsert do PostgREST é
    # um INSERT ... ON CONFLICT DO UPDATE: mesmo quando o imóvel já
    # existe e só o UPDATE vai acontecer, o Postgres valida o INSERT
    # antes — e `nome` é not null. Sem esta linha, a gravação inteira
    # morre com 23502 na primeira linha do lote.
    nome_por_id = {l["id"]: l["nome"] for l in catalogo}
    linhas, ausentes = [], []

    def texto(reg, col):
        return str(reg.get(col) or "").strip() if col else ""

    for reg in registros:
        bruto = texto(reg, c_apto)
        # Mesmo casamento tolerante das reservas: o cadastro escreve
        # "Brera127", a Stays escreve "Brera 127 -- Units: 2".
        ident = (por_nome.get(chave_nome(bruto))
                 or por_nome.get(chave_nome(normalizar_apto(bruto))))
        if not ident:
            if bruto:
                ausentes.append(bruto)
            continue
        vaga = texto(reg, c_vaga)
        tem_vaga = vaga.lower().startswith("sim")
        linhas.append({
            "id": ident,
            "nome": nome_por_id[ident],
            "empresa_limpeza": texto(reg, c_empresa) or None,
            "tem_vaga": tem_vaga,
            "vaga_detalhe": vaga if tem_vaga else None,
            "tem_facial": texto(reg, c_facial).lower().startswith("sim"),
        })

    gravar("listings", linhas, "id")
    log(f"cadastro ({origem}): {len(linhas)} imóveis atualizados")
    if ausentes:
        log(f"  não encontrados no catálogo da Stays ({len(ausentes)}): "
            + ", ".join(sorted(set(ausentes))[:15]))
    return len(linhas)


def passo_cadastro() -> int:
    """
    Traz o cadastro da tabela do Supabase, se ela existir.

    Vantagem sobre o CSV: você edita no editor de tabelas do Supabase e a
    próxima rodada já pega. Apartamento novo é uma linha, não um arquivo
    reenviado ao GitHub. Se a tabela não existir, o passo sai em silêncio
    — o CSV continua funcionando por --cadastro.
    """
    r = HTTP.get(f"{SB}/rest/v1/{TABELA_CADASTRO}", headers=SB_HEAD,
                 params={"select": "*"}, timeout=TEMPO_LIMITE)
    if r.status_code == 404:
        return 0
    if r.status_code >= 300:
        log(f"cadastro (tabela): HTTP {r.status_code} — {r.text[:200]}")
        return 0
    return aplicar_cadastro(r.json(), "tabela")


def importar_cadastro(caminho: str) -> None:
    """
    Importa o cadastro de um CSV, para quem preferir arquivo à tabela.

        python sync_stays.py --cadastro cadastro_apartamentos.csv

    O separador é detectado sozinho. Isto não é luxo: o Excel e o Sheets
    em português exportam com PONTO E VÍRGULA, porque a vírgula é o
    separador decimal. Lido como vírgula, o arquivo inteiro vira uma
    coluna só chamada "apartamento;empresa;vaga;facial" e nenhuma linha
    casa — sem erro nenhum, só zero importado.
    """
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        cabecalho = f.readline()
        f.seek(0)
        separador = max((";", ",", "\t"), key=cabecalho.count)
        if cabecalho.count(separador) == 0:
            sys.exit(f"Não achei separador no cabeçalho de {caminho}: {cabecalho[:120]}")
        leitor = csv.DictReader(f, delimiter=separador)
        leitor.fieldnames = [(c or "").strip() for c in (leitor.fieldnames or [])]
        aplicar_cadastro(list(leitor), f"csv '{separador}'")


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
    log(f"sync_stays {VERSAO}")
    run = HTTP.post(f"{SB}/rest/v1/sync_runs", headers={**SB_HEAD, "Prefer": "return=representation"},
                        data=json.dumps({"inicio": agora.isoformat()}), timeout=TEMPO_LIMITE)
    run_id = run.json()[0]["id"] if run.status_code < 300 else None

    try:
        # Catálogo: 1×/dia, ou quando pedido.
        #
        # A hora precisa cair DENTRO da janela do agendamento. Enquanto o
        # cron era 24h, 4 da manhã era ótimo: ninguém usando. Com a janela
        # de 09h à meia-noite, a hora 4 nunca acontece — e o catálogo
        # deixaria de ser atualizado para sempre, sem erro nenhum. Imóvel
        # novo simplesmente não apareceria. Por isso HORA_CATALOGO é uma
        # constante junto das outras: quem mexer no cron vê que existe.
        if args.completo or agora.hour == HORA_CATALOGO:
            passo_catalogo()
        por_id, por_nome = mapa_listings()
        if not por_id:
            passo_catalogo()
            por_id, por_nome = mapa_listings()
        validos = set(por_id.keys())

        # Cadastro antes das reservas: as etiquetas de vaga e facial que o
        # time vê saem daqui, e é barato (uma leitura).
        passo_cadastro()

        n_res = passo_reservas(validos, por_nome)

        # Reconciliação: 1×/hora. É o passo que descobre cancelamento e
        # alteração — não precisa ser a cada 15 minutos, mas não pode
        # ficar mais de uma hora sem rodar.
        n_eventos = 0
        if args.completo or agora.minute < 15:
            n_eventos = passo_reconciliacao(validos, por_nome)

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
