import os
import json
import time
import requests
import feedparser
from datetime import datetime, timezone
from urllib.parse import quote_plus

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_TO")

STATE_FILE = "state.json"

# Opcional: mandar 1 mensagem por dia dizendo que está rodando
# Ative criando uma Repository Variable (ou Secret) chamada DAILY_PING=1
DAILY_PING = os.getenv("DAILY_PING", "0") == "1"

# Termos mais “certeiros”
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
    "User-Agent": "Mozilla/5.0 (compatible; DCA-monitor/1.0; +https://github.com/RicardoBrancoDC/DCA)"
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
            if "last_ping_date" not in state:
                state["last_ping_date"] = ""
            return state
        except Exception as e:
            log(f"[WARN] Falha lendo state.json, vou recriar. Motivo: {e}")
    return {"seen_ids": [], "last_ping_date": ""}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text: str):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Faltou TELEGRAM_TOKEN ou TELEGRAM_TO nos secrets do GitHub.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": False
        },
        timeout=20
    )
    if r.status_code != 200:
        raise RuntimeError(f"Telegram retornou {r.status_code}: {r.text}")

def norm(s: str) -> str:
    return (s or "").strip().lower()

def is_relevant(title: str, summary: str) -> bool:
    blob = f"{norm(title)} {norm(summary)}"

    # bateu em termo explícito, é dentro
    if any(p in blob for p in EXPLICIT_PHRASES):
        return True

    # se aparecer "dca", exigir contexto junto
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
    # tenta achar um id estável
    eid = entry.get("id") or entry.get("guid") or entry.get("link")
    if eid:
        return eid
    # fallback: título + data
    return f"{entry.get('title','')}-{entry.get('published','')}"

def label_source(source: str) -> str:
    return "IMPRENSA" if source == "google_news" else "OPINIAO"

def maybe_daily_ping(state):
    if not DAILY_PING:
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_ping_date") == today:
        return

    msg = f"[DCA BOT] Estou rodando. Data (UTC): {today}"
    send_telegram(msg)
    state["last_ping_date"] = today
    log("[PING] Mensagem diária enviada no Telegram.")

def main():
    state = load_state()
    seen = state.get("seen_ids", [])

    log(f"Iniciando. seen_ids={len(seen)}")
    maybe_daily_ping(state)

    feeds = []
    for q in SEARCH_QUERIES:
        feeds.append(("google_news", q, google_news_rss(q)))
        feeds.append(("reddit", q, reddit_search_rss(q)))

    new_ids = []
    sent = 0
    feed_ok = 0
    feed_fail = 0

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

                if is_relevant(title, summary):
                    tag = label_source(source)
                    msg = f"[{tag}] {title}\n{link}"
                    send_telegram(msg)
                    sent += 1
                    log(f"[SEND] {tag} | {title[:120]}")
                    time.sleep(1.0)

                new_ids.append(eid)

        except Exception as e:
            feed_fail += 1
            log(f"[ERRO] {source} | query={q} | {e}")
            continue

    state["seen_ids"] = (seen + new_ids)[-400:]
    save_state(state)

    log(f"Finalizando. enviados={sent} novos_ids={len(new_ids)} feeds_ok={feed_ok} feeds_erro={feed_fail}")

if __name__ == "__main__":
    main()
