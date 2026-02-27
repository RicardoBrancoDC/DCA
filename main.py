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

# Ajustes que você pode controlar por Variables do GitHub
MAX_SEND_PER_RUN = int(os.getenv("MAX_SEND_PER_RUN", "10"))          # ex: 5, 8, 10
MAX_ENTRY_AGE_DAYS = int(os.getenv("MAX_ENTRY_AGE_DAYS", "1"))      # ex: 3, 7, 14
SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", "1.0"))

# Termos mais certeiros
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

SEARCH_QUERIES = [
    '"Defesa Civil Alerta"',
    '"Defesa Civil Alerta" MIDR',
    '"DCA" "Defesa Civil"',
    '"cell broadcast" "Defesa Civil"',
    '"Defesa Civil Alerta" "cell broadcast"',
    '"Defesa Civil Alerta" Anatel',
    '"Defesa Civil Alerta" aplicativo',
    '"Defesa Civil Alerta" população',
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DCA-monitor/1.1; +https://github.com/RicardoBrancoDC/DCA)"
}

def log(msg: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {msg}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if "seen_ids" not in state:
                state["seen_ids"] = []
            return state
        except Exception as e:
            log(f"[WARN] Falha lendo state.json, vou recriar. Motivo: {e}")
    return {"seen_ids": []}

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

    if any(p in blob for p in EXPLICIT_PHRASES):
        return True

    has_dca = (" dca" in f" {blob}") or blob.startswith("dca")
    if has_dca:
        return any(ctx in blob for ctx in DCA_CONTEXT_TERMS)

    return False

def google_news_rss(query: str) -> str:
    q = quote_plus(query)
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

def reddit_search_rss(query: str) -> str:
    q = quote_plus(query)
    return f"https://www.reddit.com/search.rss?q={q}&sort=new"

def stable_id(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link") or ""

def label_source(source: str) -> str:
    return "IMPRENSA" if source == "google_news" else "OPINIAO"

def entry_datetime_utc(entry):
    # tenta pegar data do item para filtrar por idade
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc)
    except Exception:
        return None

def main():
    state = load_state()
    seen = state.get("seen_ids", [])

    log(f"Iniciando. seen_ids={len(seen)} max_send={MAX_SEND_PER_RUN} max_age_days={MAX_ENTRY_AGE_DAYS}")

    feeds = []
    for q in SEARCH_QUERIES:
        feeds.append(("google_news", q, google_news_rss(q)))
        feeds.append(("reddit", q, reddit_search_rss(q)))

    new_ids = []
    sent = 0
    feed_ok = 0
    feed_fail = 0
    sent_links = set()

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_ENTRY_AGE_DAYS)

    for source, q, url in feeds:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()

            parsed = feedparser.parse(r.content)
            feed_ok += 1
            log(f"[OK] {source} | query={q} | itens={len(parsed.entries)}")

            for entry in parsed.entries[:10]:
                eid = stable_id(entry)
                if not eid or eid in seen or eid in new_ids:
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")
                link = entry.get("link", "")

                # marca como visto, mesmo se não enviar
                new_ids.append(eid)

                # corta coisa velha
                dt = entry_datetime_utc(entry)
                if dt and dt < cutoff:
                    continue

                if not link or link in sent_links:
                    continue

                if is_relevant(title, summary):
                    tag = label_source(source)
                    msg = f"[{tag}] {title}\n{link}"
                    send_telegram(msg)
                    sent += 1
                    sent_links.add(link)
                    log(f"[SEND] {tag} | {title[:120]}")
                    time.sleep(SLEEP_SECONDS)

                    if sent >= MAX_SEND_PER_RUN:
                        log(f"[LIMITE] Atingiu MAX_SEND_PER_RUN={MAX_SEND_PER_RUN}. Parando envios neste ciclo.")
                        break

            if sent >= MAX_SEND_PER_RUN:
                break

        except Exception as e:
            feed_fail += 1
            log(f"[ERRO] {source} | query={q} | {e}")
            continue

    state["seen_ids"] = (seen + new_ids)[-500:]
    save_state(state)

    log(f"Finalizando. enviados={sent} novos_ids={len(new_ids)} feeds_ok={feed_ok} feeds_erro={feed_fail}")

if __name__ == "__main__":
    main()
