import os
import json
import time
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urlsplit, urlunsplit, parse_qsl, urlencode

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_TO")

STATE_FILE = "state.json"

# Ajustáveis por Variables do GitHub Actions (Settings > Secrets and variables > Actions > Variables)
MAX_ENTRY_AGE_HOURS = int(os.getenv("MAX_ENTRY_AGE_HOURS", "72"))
MAX_SEND_PER_RUN = int(os.getenv("MAX_SEND_PER_RUN", "30"))
SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", "1.0"))

# quantos itens ler por feed (mesmo que não mande todos, isso aumenta chance de achar relevantes)
MAX_ENTRIES_PER_FEED = int(os.getenv("MAX_ENTRIES_PER_FEED", "50"))

# Prefixo das mensagens, para ficar com cara de plantão (CGMA / SEDEC etc)
BOT_PREFIX = os.getenv("BOT_PREFIX", "CGMA").strip()

# Termos explícitos do sistema (quando a matéria é sobre DCA / Defesa Civil Alerta / alertas no celular)
EXPLICIT_PHRASES = [
    # nomes oficiais e variações
    "defesa civil alerta",
    "defesa civil alerta dca",
    "dca defesa civil alerta",
    "cell broadcast",
    "cellbroadcast",

    # como a imprensa costuma escrever
    "alerta no celular",
    "alerta no telemóvel",
    "alerta por celular",
    "alerta via celular",
    "alerta no smartphone",
    "mensagem de emergência",
    "mensagem de alerta",
    "mensagem de alerta no celular",
    "alerta de emergência",
    "alerta de emergência no celular",

    # termos de contexto tecnológico
    "alerta 4g",
    "alerta 5g",
    "alerta por antena",
    "alerta por área",
    "alerta geolocalizado",
    "alerta em massa",
]

# "DCA" sozinho dá ruído (pode ser sigla de muita coisa). Exigir contexto forte.
DCA_CONTEXT_TERMS = [
    # instituições e programa
    "defesa civil",
    "midr",
    "ministério da integração",
    "cenad",
    "sedec",
    "governo federal",

    # tecnologia e regulação
    "cell broadcast",
    "cellbroadcast",
    "anatel",
    "operadora",
    "operadoras",
    "claro",
    "vivo",
    "tim",
    "oi",
    "4g",
    "5g",
    "sms",

    # jeito jornalístico
    "alerta no celular",
    "alerta por celular",
    "alerta via celular",
    "mensagem de emergência",
    "alerta de emergência",
    "mensagem de alerta",
    "alerta em massa",
    "alerta por área",
    "alerta geolocalizado",
]

# Gatilhos de severidade (prioridade) para plantão
SEVERITY_TRIGGERS = [
    # vítimas
    "morto", "mortos", "morte",
    "vítima", "vítimas", "vitima", "vitimas",
    "ferido", "feridos",
    "desaparecido", "desaparecidos",
    "soterrado", "soterrados",

    # afetados
    "desalojado", "desalojados",
    "desabrigado", "desabrigados",
    "ilhado", "ilhados",

    # resposta e situação oficial
    "tragédia", "tragedia",
    "calamidade", "estado de calamidade",
    "emergência", "emergencia",
    "decretou emergência", "decretou emergencia",
    "resgate", "buscas",

    # danos e medidas
    "interdição", "interdicao",
    "evacuação", "evacuacao",
    "desabamento", "desmoronamento",
    "destruiu", "destruída", "destruida",
    "ponte caiu", "queda de ponte",
    "rodovia interditada", "estrada interditada",
]

# Tags de ocorrência para ajudar o plantão a bater o olho
EVENT_TAGS = {
    "ALAGAMENTO": ["alagamento", "alagamentos", "ruas alagadas", "alagou"],
    "INUNDACAO": [
        "inundação", "inundacao", "enchente", "enchentes",
        "transbordamento", "rio transbordou", "inundar", "inundou"
    ],
    "ENXURRADA": ["enxurrada", "enxurradas", "cabeça d'água", "cabeca d'agua"],
    "DESLIZAMENTO": [
        "deslizamento", "deslizamentos", "desmoronamento", "desabamento",
        "queda de barreira", "soterramento", "desbarrancamento", "escorregamento"
    ],
    "CHUVA_FORTE": [
        "chuva intensa", "chuva forte", "temporal", "tempestade",
        "chuvas fortes", "toró", "tromba d'água", "tromba d'agua"
    ],
    "VENDAVAL": ["vendaval", "rajadas", "vento forte"],
    "RAYOS_GRANIZO": ["raio", "raios", "granizo", "queda de granizo"],
}

# Buscas principais (Google News + Reddit)
SEARCH_QUERIES = [
    # repercussão do sistema (oficial)
    '"Defesa Civil Alerta"',
    '"Defesa Civil Alerta" MIDR',
    '"DCA" "Defesa Civil"',
    '"cell broadcast" "Defesa Civil"',
    '"Defesa Civil Alerta" "cell broadcast"',
    '"Defesa Civil Alerta" Anatel',

    # repercussão do sistema (jeito imprensa)
    '("alerta" OR "mensagem") ("no celular" OR "por celular" OR "via celular" OR smartphone) ("defesa civil" OR MIDR OR CENAD)',
    '"mensagem de emergência" "defesa civil"',
    '("cell broadcast" OR "cellbroadcast") (Brasil OR Anatel OR operadoras)',
    '("alerta no celular" OR "alerta por celular") (anatel OR 4g OR 5g OR operadoras)',

    # ocorrências com possível impacto (plantão) - ampliar “impacto” além de vítimas
    '(alagamento OR inundação OR inundacao OR enchente OR enxurrada OR transbordamento) '
    '(vítimas OR vitimas OR mortos OR feridos OR desaparecidos OR desabrigados OR desalojados OR interdição OR interdicao OR evacuação OR evacuacao)',

    '(deslizamento OR desmoronamento OR "queda de barreira" OR soterramento OR desabamento) '
    '(vítimas OR vitimas OR mortos OR feridos OR desaparecidos OR desabrigados OR desalojados OR interdição OR interdicao OR evacuação OR evacuacao)',
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; cgma-monitor/1.0; +https://github.com/RicardoBrancoDC/DCA)"
}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid"
}

def log(msg: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {msg}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
    else:
        state = {}

    state.setdefault("seen_ids", [])
    state.setdefault("seen_links", [])
    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text: str):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Faltou TELEGRAM_TOKEN ou TELEGRAM_TO nos secrets do GitHub.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False},
        timeout=20
    )
    if r.status_code != 200:
        raise RuntimeError(f"Telegram retornou {r.status_code}: {r.text}")

def norm(s: str) -> str:
    return (s or "").strip().lower()

def normalize_link(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        q = [
            (k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
        ]
        query = urlencode(q, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except Exception:
        return url

def is_system_topic(title: str, summary: str) -> bool:
    blob = f"{norm(title)} {norm(summary)}"

    if any(p in blob for p in EXPLICIT_PHRASES):
        return True

    has_dca = (" dca" in f" {blob}") or blob.startswith("dca")
    if has_dca:
        return any(ctx in blob for ctx in DCA_CONTEXT_TERMS)

    return False

def is_occurrence_topic(title: str, summary: str) -> bool:
    blob = f"{norm(title)} {norm(summary)}"
    return any(w in blob for words in EVENT_TAGS.values() for w in words)

def is_impact_topic(title: str, summary: str) -> bool:
    blob = f"{norm(title)} {norm(summary)}"
    return any(t in blob for t in SEVERITY_TRIGGERS)

def is_relevant(title: str, summary: str) -> bool:
    # sistema: pega quase tudo, porque você quer repercussão
    if is_system_topic(title, summary):
        return True

    # ocorrência: exigir impacto para não virar boletim genérico
    if is_occurrence_topic(title, summary) and is_impact_topic(title, summary):
        return True

    return False

def classify_tags(title: str, summary: str):
    blob = f"{norm(title)} {norm(summary)}"
    tags = []

    if any(t in blob for t in SEVERITY_TRIGGERS):
        tags.append("PRIO")

    if is_system_topic(title, summary):
        tags.append("SISTEMA")

    for tag, words in EVENT_TAGS.items():
        if any(w in blob for w in words):
            tags.append(tag)

    # remover duplicatas mantendo ordem
    seen = set()
    out = []
    for t in tags:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out

def entry_datetime_utc(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc)
    except Exception:
        return None

def stable_id(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link") or ""

def label_source(source: str) -> str:
    if source in ("google_news", "gdelt"):
        return "IMPRENSA"
    return "OPINIAO"

def google_news_rss(query: str) -> str:
    q = quote_plus(query)
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

def reddit_search_rss(query: str) -> str:
    q = quote_plus(query)
    return f"https://www.reddit.com/search.rss?q={q}&sort=new"

def gdelt_rss(gdelt_query: str) -> str:
    # Alinhando timespan com a janela do script
    q = quote_plus(gdelt_query)
    return (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={q}"
        "&mode=ArtList"
        "&format=rss"
        "&sort=DateDesc"
        "&maxrecords=100"
        f"&timespan={MAX_ENTRY_AGE_HOURS}h"
    )

def build_feeds():
    feeds = []

    # Camada 1: Google News + Reddit, para todas as queries
    for q in SEARCH_QUERIES:
        feeds.append(("google_news", q, google_news_rss(q)))
        feeds.append(("reddit", q, reddit_search_rss(q)))

    # Camada 2: GDELT, com poucas queries “largas”
    gdelt_queries = [
        '(("Defesa Civil Alerta" OR "cell broadcast" OR DCA) sourcecountry:brazil)',

        '((alagamento OR enchente OR inundacao OR inundação OR transbordamento OR flood OR flooding OR inundation) '
        '(vitimas OR vítimas OR mortos OR feridos OR desaparecidos OR victims OR dead OR deaths OR injured OR missing '
        'OR desabrigados OR desalojados OR evacuacao OR evacuação OR interdicao OR interdição) '
        'sourcecountry:brazil)',

        '((deslizamento OR desmoronamento OR "queda de barreira" OR soterramento OR landslide OR desabamento) '
        '(vitimas OR vítimas OR mortos OR feridos OR desaparecidos OR victims OR dead OR deaths OR injured OR missing '
        'OR desabrigados OR desalojados OR evacuacao OR evacuação OR interdicao OR interdição) '
        'sourcecountry:brazil)',
    ]

    for q in gdelt_queries:
        feeds.append(("gdelt", q, gdelt_rss(q)))

    return feeds

def main():
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    seen_links = set(state.get("seen_links", []))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_ENTRY_AGE_HOURS)

    log(
        f"Iniciando. seen_ids={len(seen_ids)} seen_links={len(seen_links)} "
        f"janela_horas={MAX_ENTRY_AGE_HOURS} max_send={MAX_SEND_PER_RUN} entries_feed={MAX_ENTRIES_PER_FEED}"
    )

    feeds = build_feeds()

    new_ids = []
    new_links = []
    sent_links_this_run = set()

    sent = 0
    feed_ok = 0
    feed_fail = 0

    for source, q, url in feeds:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()

            parsed = feedparser.parse(r.content)
            feed_ok += 1
            log(f"[OK] {source} | itens={len(parsed.entries)} | query={q[:80]}")

            for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
                eid = stable_id(entry)
                if not eid:
                    continue
                if eid in seen_ids or eid in new_ids:
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")
                link = normalize_link(entry.get("link", ""))

                # marca id como visto, mesmo que não mande
                new_ids.append(eid)

                # janela: se não tiver data confiável, não manda
                dt = entry_datetime_utc(entry)
                if dt is None or dt < cutoff:
                    continue

                # não repetir link
                if not link:
                    continue
                if link in seen_links or link in new_links or link in sent_links_this_run:
                    continue

                if is_relevant(title, summary):
                    src = label_source(source)
                    tags = classify_tags(title, summary)
                    tag_txt = " ".join([f"[{t}]" for t in tags]) if tags else ""

                    msg = f"[{BOT_PREFIX}] [{src}] {tag_txt} {title}\n{link}".strip()
                    send_telegram(msg)

                    sent += 1
                    new_links.append(link)
                    sent_links_this_run.add(link)
                    log(f"[SEND] {src} | {', '.join(tags) if tags else 'SEM_TAG'} | {title[:110]}")
                    time.sleep(SLEEP_SECONDS)

                    if sent >= MAX_SEND_PER_RUN:
                        log(f"[LIMITE] MAX_SEND_PER_RUN={MAX_SEND_PER_RUN}. Parando envios neste ciclo.")
                        break

            if sent >= MAX_SEND_PER_RUN:
                break

        except Exception as e:
            feed_fail += 1
            log(f"[ERRO] {source} | {e}")
            continue

    # memória: manter um histórico razoável
    state["seen_ids"] = (list(seen_ids) + new_ids)[-1200:]
    state["seen_links"] = (list(seen_links) + new_links)[-2000:]
    save_state(state)

    log(f"Finalizando. enviados={sent} novos_ids={len(new_ids)} novos_links={len(new_links)} feeds_ok={feed_ok} feeds_erro={feed_fail}")

if __name__ == "__main__":
    main()
