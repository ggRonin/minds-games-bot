"""minds.games — регистрация email по реферальной ссылке.

Как работает сайт (index.html):
  1. ?ref=CODE в URL сохраняется в localStorage['mg_ref'] (UPPERCASE, 6-12 символов A-Za-z0-9)
  2. форма шлёт POST /api/signup {email, website:"", consent:true, turnstile, ref}
     (website — honeypot, должен быть пустым; turnstile — токен Cloudflare Turnstile)
  3. в ответе: seat, referral_code, session; кука mg_me (HttpOnly, 30 дней) — сессия аккаунта

Бот повторяет ровно этот путь: заходит по реф-ссылке, забирает куки/флаги,
решает Turnstile через CapSolver и отправляет signup с ref.
"""
import argparse
import asyncio
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

# Windows-консоль по умолчанию cp1251/cp1252 — не ломаемся на «→», «✅» и кириллице
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import config
except ImportError:
    print("config.py не найден — создай его по образцу config.py.example")
    sys.exit(1)

BASE_URL           = "https://minds.games"
TURNSTILE_SITEKEY  = "0x4AAAAAAE0HxEwcxxbWFxYt"
CAPSOLVER_BASE     = "https://api.capsolver.com"
STATE_FILE         = Path("state.json")
LIMIT_MIN, LIMIT_MAX = 130, 250   # сколько успешных регистраций делаем за запуск (случайно в этом диапазоне)
REF_FILE           = Path(__file__).with_name("Reff.txt")
EMAILS_FILE        = Path(__file__).with_name("emails.txt")
PROXY_FILE         = Path(__file__).with_name("Proxy.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
SEC_CH = {
    "sec-ch-ua":          '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
}
PAGE_HEADERS = {
    "User-Agent":      UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Sec-Fetch-User":  "?1",
    **SEC_CH,
}
API_HEADERS = {
    "User-Agent":      UA,
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          BASE_URL,
    "Referer":         BASE_URL + "/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    **SEC_CH,
}


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Config helpers ────────────────────────────────────────────────────────────
def _capsolver_key() -> str:
    return getattr(config, "CAPSOLVER_API_KEY", "")


def load_refs() -> list[str]:
    """Реф-коды из Reff.txt: по одному на строку — либо ссылка ...?ref=CODE, либо сам код."""
    if not REF_FILE.exists():
        return []
    refs = []
    for line in REF_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.search(r"[?&]ref=([A-Za-z0-9]+)", line)
        refs.append((m.group(1) if m else line).upper())
    return refs


def _err(e: Exception) -> str:
    """Текст ошибки без логина:пароля прокси (aiohttp кладёт полный URL прокси в сообщение)
    и с именем класса — у таймаутов str(e) пустой."""
    msg = re.sub(r"//[^/@\s]+@", "//***@", str(e))
    return f"{type(e).__name__}: {msg}"


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = (l.strip() for l in path.read_text(encoding="utf-8-sig").splitlines())
    return [l for l in lines if l and not l.startswith("#")]


def pop_proxy() -> str | None:
    """Берёт первый прокси из Proxy.txt и сразу удаляет его из файла — один прокси на один аккаунт."""
    lines = load_lines(PROXY_FILE)
    if not lines:
        return None
    tmp = PROXY_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines[1:]) + ("\n" if len(lines) > 1 else ""), encoding="utf-8")
    tmp.replace(PROXY_FILE)
    return lines[0]


def fmt_proxy(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    return raw if "://" in raw else f"http://{raw}"


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(data: dict):
    STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Turnstile (CapSolver) ─────────────────────────────────────────────────────
async def solve_turnstile(session: aiohttp.ClientSession) -> str:
    key = _capsolver_key()
    if not key:
        log("CAPSOLVER_API_KEY не задан — впиши его в config.py")
        return ""
    try:
        async with session.post(
            f"{CAPSOLVER_BASE}/createTask",
            json={
                "clientKey": key,
                "task": {
                    "type":       "AntiTurnstileTaskProxyLess",
                    "websiteURL": BASE_URL + "/",
                    "websiteKey": TURNSTILE_SITEKEY,
                },
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            data = await r.json()
    except Exception as e:
        log(f"CapSolver create error: {e}")
        return ""

    if data.get("errorId", 0) != 0:
        log(f"CapSolver error: {data.get('errorCode')} — {data.get('errorDescription')}")
        return ""
    task_id = data.get("taskId")
    if not task_id:
        return ""
    log(f"CapSolver taskId={task_id}, решаем...")

    for attempt in range(40):
        await asyncio.sleep(3)
        try:
            async with session.post(
                f"{CAPSOLVER_BASE}/getTaskResult",
                json={"clientKey": key, "taskId": task_id},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                result = await r.json()
        except Exception as e:
            log(f"CapSolver poll error: {e}")
            continue
        status = result.get("status")
        if status == "ready":
            log(f"Turnstile решён за ~{(attempt + 1) * 3}s")
            return result.get("solution", {}).get("token", "")
        if status == "failed":
            log(f"Turnstile failed: {result.get('errorDescription')}")
            return ""
    log("Turnstile timeout (120s)")
    return ""


# ── Signup ────────────────────────────────────────────────────────────────────
async def register(email: str, ref: str, proxy: str | None) -> bool:
    ref = ref.strip().upper()
    if not re.fullmatch(r"[A-Za-z0-9]{6,12}", ref):
        log(f"Некорректный ref-код: {ref!r} (сайт принимает 6-12 символов A-Za-z0-9)")
        return False

    state = _read_state()
    if state.get(email, {}).get("ok"):
        s = state[email]
        log(f"{email} уже зарегистрирован (seat #{s.get('seat')}, ref-код {s.get('referral_code')}). "
            f"Удали запись из state.json, чтобы повторить.")
        return True

    jar = aiohttp.CookieJar()
    connector = aiohttp.TCPConnector(limit=5)
    timeout = aiohttp.ClientTimeout(total=45, connect=20)
    kw = {"proxy": proxy} if proxy else {}

    async with aiohttp.ClientSession(connector=connector, timeout=timeout, cookie_jar=jar) as session:
        # 1. Заходим по реф-ссылке, как реальный посетитель (JS сайта кладёт ref в localStorage)
        url = f"{BASE_URL}/?ref={ref}"
        try:
            async with session.get(url, headers=PAGE_HEADERS, **kw) as r:
                await r.read()
                log(f"GET {url} → {r.status}")
                if r.status != 200:
                    return False
            # 2. Как и сайт — запрашиваем флаги фазы кампании
            async with session.get(
                f"{BASE_URL}/api/flags",
                headers={**API_HEADERS, "Cache-Control": "no-store"}, **kw,
            ) as r:
                flags = await r.json(content_type=None)
                log(f"GET /api/flags → {r.status} {flags}")
        except Exception as e:
            log(f"Ошибка загрузки страницы (прокси?): {_err(e)}")
            return False

        # 3. Turnstile
        token = await solve_turnstile(session)
        if not token:
            return False

        # 4. Signup — тело идентично тому, что шлёт форма
        body = {"email": email, "website": "", "consent": True, "turnstile": token, "ref": ref}
        try:
            async with session.post(
                f"{BASE_URL}/api/signup",
                data=json.dumps(body, separators=(",", ":")),
                headers={**API_HEADERS, "Content-Type": "application/json"},
                **kw,
            ) as r:
                text = await r.text()
                log(f"POST /api/signup → {r.status} {text[:300]}")
                try:
                    j = json.loads(text)
                except Exception:
                    j = {}
                if r.status != 200 or not j.get("ok"):
                    return False
        except Exception as e:
            log(f"Ошибка signup: {_err(e)}")
            return False

        cookies = {c.key: c.value for c in jar}
        state[email] = {
            "ok":            True,
            "ref_used":      ref,
            "referred":      j.get("referred"),
            "seat":          j.get("seat"),
            "referral_code": j.get("referral_code"),
            "session":       j.get("session"),
            "cookies":       cookies,
            "registered_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_state(state)

    if j.get("referred"):
        log(f"✅ Готово: {email} | seat #{j.get('seat')} | реф засчитан (referred=true) | "
            f"свой ref-код: {j.get('referral_code')}")
    else:
        log(f"⚠ Зарегистрирован, но referred=false — реф НЕ засчитан. seat #{j.get('seat')}")
    return True


async def run_all(emails: list[str], refs: list[str], fixed_proxy: str) -> int:
    """Реф-коды идут по очереди: на каждый — случайное число регистраций (LIMIT_MIN..LIMIT_MAX),
    потом переходим к следующему, пока не кончатся рефы, email'ы или прокси."""
    total = 0
    idx = 0
    for r, ref in enumerate(refs, 1):
        limit = random.randint(LIMIT_MIN, LIMIT_MAX)
        log(f"══ Реф {r}/{len(refs)}: {ref} — лимит регистраций {limit}")
        done = 0
        while done < limit and idx < len(emails):
            email = emails[idx]
            idx += 1
            proxy = fixed_proxy or pop_proxy()
            if not proxy:
                log(f"{PROXY_FILE.name} пуст — останавливаемся (без прокси не работаем, чтобы не светить свой IP)")
                return total
            log(f"── [{idx}/{len(emails)}] {email} | ref {ref} | proxy {proxy.split('@')[-1]}")
            if await register(email, ref, fmt_proxy(proxy)):
                done += 1
                total += 1
        log(f"Реф {ref}: зарегистрировано {done}/{limit}")
        if idx >= len(emails):
            break
    return total


def main():
    p = argparse.ArgumentParser(description="minds.games referral signup")
    p.add_argument("--email", default="", help="один email вместо списка из emails.txt")
    p.add_argument("--ref",   default="", help="один реф-код вместо списка из Reff.txt")
    p.add_argument("--proxy", default="", help="конкретный прокси (по умолчанию берётся и удаляется из Proxy.txt)")
    a = p.parse_args()

    emails = [a.email.strip()] if a.email else load_lines(EMAILS_FILE)
    # уже зарегистрированные пропускаем до взятия прокси, чтобы не сжигать их впустую
    state = _read_state()
    todo = [e for e in emails if not state.get(e, {}).get("ok")]
    log(f"Email: всего {len(emails)}, уже готово {len(emails) - len(todo)}, к регистрации {len(todo)}")
    if not todo:
        return

    refs = [a.ref.strip().upper()] if a.ref else list(dict.fromkeys(load_refs()))
    if not refs:
        print(f"Нет реф-кода: заполни {REF_FILE.name} или передай --ref")
        sys.exit(1)

    done = asyncio.run(run_all(todo, refs, a.proxy))
    log(f"Итого зарегистрировано: {done} из {len(todo)} в очереди")
    sys.exit(0 if done else 1)


if __name__ == "__main__":
    main()
