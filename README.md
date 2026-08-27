# Recepção Innvista — sistema novo

Substitui a planilha `dashboard recepção`. Três arquivos, nenhuma etapa de
compilação, nenhum framework.

```
schema.sql      o banco            → cola no Supabase e roda
sync_stays.py   a sincronização    → roda sozinha no GitHub Actions
painel.html     a tela do time     → um arquivo, hospedado no Cloudflare
```

Custo: **R$ 0/mês** nos planos gratuitos, com folga grande para 113 imóveis.

---

## A ideia em uma frase

O robô e o time escrevem em tabelas **diferentes**, e o banco impede que um
alcance a do outro. Toda a complexidade do `mergeComPreservacao` da planilha —
as "colunas intocáveis", as proteções reforçadas de cada versão de 2.1 a 2.8 —
existia para mediar um conflito que aqui simplesmente não acontece.

---

## Ordem de instalação

Faça na ordem. Cada passo depende do anterior.

### 1. Banco (15 min)

1. Crie a conta em [supabase.com](https://supabase.com) → **New project**.
   - Região: **South America (São Paulo)** — menor latência para o time.
   - Guarde a senha do banco num gerenciador de senhas. Ela não é recuperável.
2. **SQL Editor** → cole o `schema.sql` inteiro → **Run**.
3. Pegue a URL e as chaves. **Atenção: isso mudou de lugar recentemente.**
   Não é mais `Settings → API`, e sim:

   > **Settings** (engrenagem, canto inferior esquerdo) → **API Keys**

   A URL do projeto fica em **Settings → General**, ou no botão **Connect**
   no topo da tela, que mostra tudo junto e costuma ser o caminho mais curto.

   Na página **API Keys** há **duas abas**, e a diferença importa:

   | Aba | Chaves | Situação |
   |---|---|---|
   | *Publishable and secret API keys* | `sb_publishable_…` e `sb_secret_…` | **use estas** |
   | *Legacy API Keys* | `anon` e `service_role` (formato JWT, começam com `eyJ…`) | funcionam, mas são descontinuadas no fim de 2026 |

   Anote três coisas:

   | O quê | Onde | Vai para |
   |---|---|---|
   | URL do projeto | Settings → General, ou botão Connect | `SUPABASE_URL` |
   | `sb_publishable_…` | aba *Publishable and secret* | o bloco CONFIG do `painel.html` |
   | `sb_secret_…` | mesma aba, botão de revelar | Secrets do GitHub, e **só lá** |

   > ⚠️ **A URL do projeto termina em `.supabase.co` e mais nada.** Algo como
   > `https://abcdefgh.supabase.co`. Se você copiar de um trecho de código do
   > painel, vem `https://abcdefgh.supabase.co/rest/v1` junto — e aí o caminho
   > fica duplicado, com um erro (`PGRST125 — Invalid path specified in request
   > URL`) que fala de caminho e não de URL, o que atrapalha na hora de
   > entender. O script corta esse sufixo sozinho, mas é melhor guardar limpo.

   **A publishable pode ficar visível no HTML** — ela é pública por natureza, e
   quem protege os dados é o row level security do banco, não o segredo da
   chave. A `sb_secret_` nunca entra no HTML nem no repositório.

   > Se você só encontrar as chaves antigas (`anon` e `service_role`), pode usar:
   > o `sync_stays.py` aceita os dois formatos e ajusta os cabeçalhos sozinho.
   > Mas prefira as novas, porque as antigas param de funcionar no fim de 2026 —
   > o que é logo ali.

Confira que funcionou rodando no SQL Editor:

```sql
-- deve devolver zero linhas: o robô não enxerga as tarefas do time
select grantee, privilege_type from information_schema.role_table_grants
 where table_name = 'tasks' and grantee = 'service_role';
```

Se devolver linhas, o `REVOKE` não pegou — me chame antes de seguir.

> Por que a consulta fala em `service_role` mesmo usando a chave nova: a
> `sb_secret_` continua entrando no banco com esse mesmo papel do Postgres.
> Mudou o formato da chave, não o papel por trás dela — então o `REVOKE`
> do `schema.sql` vale para as duas.

### 2. Sincronização (20 min)

Dá para fazer tudo pelo navegador, sem instalar git nem baixar arquivo nenhum.

1. Crie um repositório **privado** no GitHub (ex.: `innvista-recepcao`).
   Marque *Add a README file* — o repositório precisa ter pelo menos um arquivo
   para o botão de criar os próximos aparecer.

2. Crie os três arquivos, um por vez, sempre em **Add file → Create new file**.

   O truque que resolve a pasta `.github`: **você não cria pasta no GitHub.**
   Digita o caminho inteiro no campo do nome, com barras, e ele cria sozinho.
   Ao digitar a primeira `/`, a barra vira uma pasta na hora, na sua frente.

   | No campo do nome, digite | Cole dentro |
   |---|---|
   | `sync_stays.py` | o conteúdo do arquivo |
   | `requirements.txt` | uma linha: `requests>=2.31` |
   | `.github/workflows/sync.yml` | o conteúdo do `sync.yml` |

   Em cada um, role até o fim e clique em **Commit changes**.

   > **`.gitignore` é opcional** e você pode pular. Ele só serve para o git
   > ignorar arquivos quando se trabalha no computador — trabalhando pelo
   > navegador, não muda nada. Se quiser criar mesmo assim, o nome é
   > `.gitignore` (com o ponto na frente) e o conteúdo são cinco linhas:
   > `.venv/`, `__pycache__/`, `*.pyc`, `cadastro*.csv`, `.env`.

   O caminho `.github/workflows/sync.yml` precisa ser **exatamente** esse. É
   ali que o GitHub procura por automações; um `sync.yml` solto na raiz do
   repositório é ignorado em silêncio, sem nenhum aviso.

3. **Settings → Secrets and variables → Actions → New repository secret**,
   cinco vezes:

   | Nome | Valor |
   |---|---|
   | `STAYS_DOMAIN` | `https://innvista.stays.net` |
   | `STAYS_CLIENT_ID` | o par dedicado ao dashboard |
   | `STAYS_CLIENT_SECRET` | idem |
   | `SUPABASE_URL` | do passo 1 |
   | `SUPABASE_SERVICE_KEY` | a chave `sb_secret_…` (ou a `service_role`, se usar as legadas) |

4. **Actions → sync → Run workflow**, marcando *Forçar catálogo e reconciliação*.
5. Acompanhe o log. Deve terminar em menos de um minuto.

Confira no Supabase → **Table Editor**: `listings` com ~113 linhas,
`reservations` com algumas centenas, `blocks` com algumas dezenas.

### 3. Cadastro dos imóveis (10 min)

O catálogo da Stays não sabe qual empresa limpa cada apartamento, se tem vaga
nem se tem facial. Isso vive no seu `cadastro_apartamentos` e precisa entrar
uma vez.

1. Abra o `cadastro_apartamentos`, aba `Auxiliar pós cadastro`.
2. Copie as quatro colunas que interessam para uma planilha nova, com estes
   nomes exatos no cabeçalho: `apartamento`, `empresa`, `vaga`, `facial`.
   *(Hoje são as colunas O, J, I e P — mas o script passa a usar o nome, não a
   posição. É por isso que arrastar uma coluna deixa de quebrar tudo.)*
3. `Arquivo → Fazer download → Valores separados por vírgula (.csv)`.
   Vai para a sua pasta de Downloads.

4. **Suba o CSV para o repositório** — pelo navegador, como os outros arquivos:
   **Add file → Upload files**, arraste o CSV, **Commit changes**.
   Anote o nome exato do arquivo (ex.: `cadastro.csv`).

5. **Actions → sync → Run workflow.** No campo *Importar cadastro*, digite o
   nome do arquivo. Deixe o *Forçar catálogo* desmarcado. **Run workflow**.

O log mostra quantos imóveis foram atualizados e lista os que ele não achou no
catálogo da Stays — normalmente os desativados, que podem ser ignorados:

```
cadastro: 113 imóveis atualizados
  não encontrados no catálogo da Stays (5): Extension 135, Uwin 1713, ...
```

Depois de importar, o CSV já cumpriu o papel — se preferir, apague do
repositório (abra o arquivo → ícone de lixeira → Commit).

> **Por que não rodar no seu computador:** dava para fazer com Python
> instalado e três comandos no terminal, mas seria a única etapa do sistema
> inteiro a exigir isso. Rodando aqui, tudo vive no mesmo lugar: os Secrets já
> estão configurados, o log fica registrado, e daqui a um ano você repete o
> processo sem precisar lembrar de nada.

### 4. Painel (15 min)

1. Abra o `painel.html` e preencha o bloco `CONFIG` no topo com a `SUPABASE_URL`
   e a chave **`sb_publishable_…`** (ou a `anon`, se estiver usando as legadas).
   Se colar a `sb_secret_` aqui por engano, o navegador recebe 401 em tudo — é
   proposital, o Supabase recusa chave secreta vinda de página web.
2. No Supabase → **Authentication → Providers → Google**, ative e restrinja ao
   domínio da Innvista.
3. **Authentication → Users → Add user** para cada pessoa da recepção.
4. Suba o arquivo em [pages.cloudflare.com](https://pages.cloudflare.com) →
   *Upload assets*, ou conecte no repositório do GitHub para publicar a cada
   `git push`.

---

## Conferência antes de virar a chave

**Não migre o time no mesmo dia.** Deixe os dois rodando em paralelo e compare
por sete dias seguidos. O critério para avançar é bater sete vezes, não uma.

Consulta útil no SQL Editor:

```sql
-- check-ins de hoje, com o estado da limpeza já resolvido
select apartamento, hospede_nome, limpeza_estado, ultima_saida, ultima_saida_origem
  from v_checkin where check_in = current_date order by apartamento;
```

Compare com a aba Checkin da planilha. Divergência aqui é sinal de que algo no
mapeamento está errado — e é muito melhor descobrir agora.

---

## Operação do dia a dia

| O que | Onde |
|---|---|
| A sincronização falhou? | tabela `sync_runs`, ou a aba Actions do GitHub |
| Quem marcou o quê | tabela `audit_log` |
| Rodar fora de hora | Actions → sync → Run workflow |
| Mudar a janela de busca | constantes no topo do `sync_stays.py` |
| Novo imóvel no portfólio | entra sozinho no próximo catálogo; depois preencha empresa/vaga/facial |

### Backup

O plano gratuito do Supabase **não faz backup automático** — é a única
fragilidade real dessa escolha. Solução de custo zero: uma vez por semana,
`Database → Backups → Download`, e guarde no Drive. Cinco minutos.

Se um dia quiser backup gerenciado e recuperação para qualquer ponto no tempo,
é aí que entra o plano de US$ 25/mês. Não antes.

---

## O que ainda não vem da API

A API externa da Stays **não expõe o módulo de tarefas** onde as empresas de
limpeza atuam. Os grupos que existem são booking, listings, calendar, finance,
clients e promo-codes.

Por isso a confirmação da limpeza continua sendo um ato humano: o time muda o
`status` da linha em `cleanings` de `pendente` para `feita` ou `liberada`. A
diferença para a planilha é que agora isso fica registrado com quem e quando,
e alimenta a regra do check-in automaticamente.

Vale abrir um chamado na Stays perguntando por um endpoint de tarefas fora da
documentação pública. Se existir, muda-se um passo do `sync_stays.py` e o time
para de precisar marcar à mão.

---

## Estrutura do banco

```
listings ──┬── reservations ──┐
           ├── blocks ────────┼── saidas (view)
           └── cleanings      └── ultima_saida (view)
                                      │
                                 v_checkin (view)  ← a regra da limpeza mora aqui
tasks         (só o time escreve)
audit_log     (quem mudou o quê)
sync_runs     (execuções)
```

A regra do "esse apartamento está limpo?" é um `CASE` dentro da view
`v_checkin`. Não há cache para expirar nem código para manter sincronizado:
a resposta é calculada na hora, a partir do dado que existe.

Na planilha, essa mesma pergunta exigia caminhar para trás na API mês a mês,
com cache de seis horas, porque não havia histórico confiável para consultar.
