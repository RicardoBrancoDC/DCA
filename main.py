import os
import json
import time
import requests
import feedparser
from urllib.parse import quote_plus

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_TO")

STATE_FILE = "state.json"

# Termos principais
EXPLICIT_TERMS = [
    "defesa civil alerta",
    "defesa civil alerta (dca)",
    "cell broadcast",
    "defesa civil alerta cell broadcast",
]

# "DCA" sozinho pode virar ruído. Então exigimos contexto.
CONTEXT_TERMS = [
    "defesa civil",
    "midr",
    "cenad",
    "alerta",
    "cell broadcast",
    "anatel",
    "mensagem de emergência",
]

SEARCH_QUERIES = [
    '"Defesa Civil Alerta"',
    '"Defesa Civil Alerta" MIDR',
    '"DCA" "Defesa Civil"',
    '"cell broadcast" "Defesa Civil"',
    '"Defesa Civil Alerta" "cell broadcast"',
    '"Defesa Civil Alerta" Anatel',
]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": []}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Faltou TELEGRAM_TOKEN ou TELEGRAM_TO nos secrets do GitHub.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False},
        timeout=20,
    )

def norm(s: str) -> str:
    return (s or "").strip().lower()

def is_relevant(title: str, summary: str) -> bool:
    blob = f"{norm(title)} {norm(summary)}"

    # 1) bateu em termo explícito, ok
    if any(t in blob for t in EXPLICIT_TERMS):
        return True

    # 2) se aparecer DCA, exige contexto
    if " dca" in f" {blob}" or blob.startswith("dca"):
        return any(ctx in blob for ctx in CONTEXT_TERMS)

    return False

def google_news_rss(query: str) -> str:
    q = quote_plus(query)
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

def reddit_search_rss(query: str) -> str:
    q = quote_plus(query)
    return f"https://www.reddit.com/search.rss?q={q}&sort=new"

def stable_id(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link")

def label_source(source: str) -> str:
    return "IMPRENSA" if source == "google_news" else "OPINIÃO"

def main():
    state = load_state()
    seen = state.get("seen_ids", [])

    headers = {"User-Agent": "Mozilla/5.0"}

    new_ids = []
    sent = 0

    feeds = []
    for q in SEARCH_QUERIES:
        feeds.append(("google_news", q, google_news_rss(q)))
        feeds.append(("reddit", q, reddit_search_rss(q)))

    for source, q, url in feeds:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)

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
                    time.sleep(1.0)

                new_ids.append(eid)

        except Exception:
            continue

    state["seen_ids"] = (seen + new_ids)[-400:]
    save_state(state)

if __name__ == "__main__":
    main()
