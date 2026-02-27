import os
import json
import time
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_TO")

STATE_FILE = "state.json"

# Ajustáveis por Variables do GitHub Actions (Settings > Secrets and variables > Actions > Variables)
MAX_ENTRY_AGE_HOURS = int(os.getenv("MAX_ENTRY_AGE_HOURS", "24"))
MAX_SEND_PER_RUN = int(os.getenv("MAX_SEND_PER_RUN", "8"))
SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", "1.0"))

# Prefixo das mensagens, para ficar com cara de plantão (CGMA / SEDEC etc)
BOT_PREFIX = os.getenv("BOT_PREFIX", "CGMA").strip()

# Termos explícitos do sistema (quando a matéria é sobre o DCA / Defesa Civil Alerta)
EXPLICIT_PHRASES = [
    "defesa civil alerta",
    "defesa civil alerta (dca)",
    "cell broadcast",
    "defesa civil alerta cell broadcast",
]

# "DCA" sozinho dá ruído. Exigir contexto.
DCA_CONTEXT_TERMS = [
    "defesa civil",
    "midr",
    "cenad",
    "alerta",
    "cell broadcast",
    "anatel",
    "mensagem de emergência",
    "alerta de emergência",
    "defesa civil alerta",
]

# Gatilhos de severidade (prioridade)
SEVERITY_TRIGGERS = [
    "morto", "mortos", "morte",
    "vítima", "vítimas", "vitima", "vitimas",
    "ferido", "feridos",
    "desaparecido", "desaparecidos",
    "soterrado", "soterrados",
    "tragédia", "tragedia",
    "calamidade", "estado de calamidade",
    "emergência", "emergencia",
    "interdição", "interdicao",
    "evacuação", "evacuacao",
]

# Tags de ocorrência para ajudar o plantão a bater o olho
EVENT_TAGS = {
    "ALAGAMENTO": ["alagamento", "alagamentos"],
    "INUNDACAO": ["inundação", "inundacao", "enchente", "enchentes", "transbordamento", "inundar", "inundou"],
    "ENXURRADA": ["enxurrada", "enxurradas"],
    "DESLIZAMENTO": ["deslizamento", "deslizamentos", "desmoronamento", "desabamento", "queda de barreira", "soterramento", "desbarrancamento"],
    "CHUVA_FORTE": ["chuva intensa", "chuva forte", "temporal", "tempestade", "chuvas fortes"],
}

# Buscas principais (Google News + Reddit)
SEARCH_QUERIES = [
    # repercussão do sistema
    '"Defesa Civil Alerta"',
    '"Defesa Civil Alerta" MIDR',
    '"DCA" "Defesa Civil"',
    '"cell broadcast" "Defesa Civil"',
    '"Defesa Civil Alerta" "cell broadcast"',
    '"Defesa Civil Alerta" Anatel',

    # ocorrências com possível impacto (plantão)
    '(alagamento OR inundação OR enchente OR enxurrada OR transbordamento) (vítimas OR mortos OR feridos OR desaparecidos)',
    '(deslizamento OR desmoronamento OR "queda de barreira" OR soterramento) (vítimas OR mortos OR feridos OR desaparecidos)',
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; cgma-monitor/1.0; +https://github.com/RicardoBrancoDC/DCA)"
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

def is_relevant(title: str, summary: str) -> bool:
    blob = f"{norm(title)} {norm(summary)}"

    # 1) pegou termo explícito do sistema, ok
    if any(p in blob for p in EXPLICIT_PHRASES):
        return True

    # 2) se vier "dca", exigir contexto
    has_dca = (" dca" in f" {blob}") or blob.startswith("dca")
    if has_dca:
        return any(ctx in blob for ctx in DCA_CONTEXT_TERMS)

    # 3) se bater em termos de ocorrência, ok também
    for words in EVENT_TAGS.values():
        if any(w in blob for w in words):
            return True

    return False

def classify_tags(title: str, summary: str):
    blob = f"{norm(title)} {norm(summary)}"
    tags = []

    if any(t in blob for t in SEVERITY_TRIGGERS):
        tags.append("PRIO")

    for tag, words in EVENT_TAGS.items():
        if any(w in blob for w in words):
            tags.append(tag)

    return tags

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
    # gdelt conta como imprensa
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
    # DOC API em RSS só funciona no modo ArtList
    # timespan=24h garante recorte curto; e ainda fazemos checagem de data pelo RSS
    q = quote_plus(gdelt_query)
    return (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={q}"
        "&mode=ArtList"
        "&format=rss"
        "&sort=DateDesc"
        "&maxrecords=50"
        "&timespan=24h"
    )

def build_feeds():
    feeds = []

    # Camada 1: Google News + Reddit, para todas as queries
    for q in SEARCH_QUERIES:
        feeds.append(("google_news", q, google_news_rss(q)))
        feeds.append(("reddit", q, reddit_search_rss(q)))

    # Camada 2: GDELT, com poucas queries bem “largas” (pra não ficar chamando demais)
    # Importante: restringindo para imprensa do Brasil usando sourcecountry
    gdelt_queries = [
        # repercussão do sistema
        '(("Defesa Civil Alerta" OR "cell broadcast" OR DCA) sourcecountry:brazil)',

        # ocorrências com impacto, misturando português e inglês, porque a GDELT trabalha muito com tradução
        '((alagamento OR enchente OR inundacao OR inundação OR transbordamento OR flood OR flooding OR inundation) '
        '(vitimas OR vítimas OR mortos OR feridos OR desaparecidos OR victims OR dead OR deaths OR injured OR missing) '
        'sourcecountry:brazil)',

        '((deslizamento OR desmoronamento OR "queda de barreira" OR soterramento OR landslide) '
        '(vitimas OR vítimas OR mortos OR feridos OR desaparecidos OR victims OR dead OR deaths OR injured OR missing) '
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

    log(f"Iniciando. seen_ids={len(seen_ids)} seen_links={len(seen_links)} janela_horas={MAX_ENTRY_AGE_HOURS} max_send={MAX_SEND_PER_RUN}")

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

            for entry in parsed.entries[:10]:
                eid = stable_id(entry)
                if not eid:
                    continue
                if eid in seen_ids or eid in new_ids:
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")
                link = entry.get("link", "")

                # marca id como visto, mesmo que não mande
                new_ids.append(eid)

                # janela: se não tiver data confiável, não manda (evita “coisa velha” escapando)
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
