#!/usr/bin/env python3
"""Kiểm tra cảnh báo giá và gửi Telegram / báo động ntfy (chạy bởi GitHub Actions sau mỗi lần cập nhật giá).

Đọc:
  data/market.json                                   giá mới nhất (vừa cập nhật)
  Firestore data/users/{uid}/profile                 cảnh báo của từng người (field "alerts")
  Firestore data/users/{uid}/profile/notify/settings cài đặt nhận cảnh báo (app ghi)
Ghi:
  Firestore data/users/{uid}/profile/notify/state    trạng thái từng cảnh báo phía máy chủ, chat id Telegram
  data/notify.json                                   tên bot Telegram (công khai, để app hiện đường dẫn)

Bí mật (GitHub Secrets): FIREBASE_SERVICE_ACCOUNT (JSON khoá dịch vụ), TELEGRAM_BOT_TOKEN.
Thiếu bí mật → bỏ qua, không làm hỏng lần cập nhật giá.
NOTIFY_TEST=1 → gửi tin nhắn (và báo động ntfy nếu bật) thử nghiệm cho mọi người đã cài đặt.

Mỗi cảnh báo chỉ báo MỘT lần mỗi khi giá vượt ngưỡng; giá quay lại thì sẵn sàng báo lần sau.
"""
import json
import re
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKET = ROOT / "data" / "market.json"
NOTIFY_PUBLIC = ROOT / "data" / "notify.json"
NOTIFY_LOG = ROOT / "data" / "notify-log.json"  # tóm tắt chẩn đoán công khai, không chứa tên/mã kênh
APP_URL = "https://thailandsinh1991-arch.github.io/silver-tracker/#canh-bao"
VN = timezone(timedelta(hours=7))
KINDS = {
    "buy_above": ("buy", 1, "Giá thu mua Phú Quý"),
    "buy_below": ("buy", -1, "Giá thu mua Phú Quý"),
    "sell_above": ("sell", 1, "Giá bán ra Phú Quý"),
    "sell_below": ("sell", -1, "Giá bán ra Phú Quý"),
    "world_above": ("world", 1, "Giá bạc thế giới"),
    "world_below": ("world", -1, "Giá bạc thế giới"),
}
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/")
LAST_CALL = {}


def log(*a):
    print(*a, flush=True)


def http_json(url, data=None, timeout=20):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "silver-tracker-alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_text(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "silver-tracker-alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def vnd(n):
    return f"{int(round(n)):,}".replace(",", ".") + " ₫"


def usd(n):
    return f"{n:.2f} USD/oz"


def fmt_time(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(VN).strftime("%H:%M %d/%m")
    except ValueError:
        return ""


class Telegram:
    def __init__(self, token):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}/"
        self._updates = None

    def me(self):
        return http_json(self.base + "getMe").get("result") or {}

    def updates(self):
        if self._updates is None:
            try:
                self._updates = http_json(self.base + "getUpdates?limit=100&allowed_updates=%5B%22message%22%5D").get("result") or []
            except Exception as e:  # noqa: BLE001
                log("Telegram getUpdates lỗi:", e)
                self._updates = []
        return self._updates

    def find_chat(self, username):
        u = username.lstrip("@").lower()
        for upd in reversed(self.updates()):
            m = upd.get("message") or {}
            frm = m.get("from") or {}
            if (frm.get("username") or "").lower() == u and (m.get("chat") or {}).get("type") == "private":
                return m["chat"]["id"]
        return None

    def send(self, chat_id, text):
        r = http_json(self.base + "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
        if not r.get("ok"):
            raise RuntimeError(r.get("description") or "sendMessage thất bại")


def alarm(topic, title, text, test=False):
    """Báo động reo qua ứng dụng ntfy (miễn phí, mã nguồn mở). Mức ưu tiên 5 = khẩn cấp."""
    body = json.dumps({
        "topic": topic, "title": title, "message": text, "priority": 5,
        "tags": ["rotating_light"] if not test else ["white_check_mark"], "click": APP_URL,
    }).encode("utf-8")
    req = urllib.request.Request(NTFY_URL, data=body, headers={"Content-Type": "application/json", "User-Agent": "silver-tracker-alerts"})
    with urllib.request.urlopen(req, timeout=30) as r:
        reply = json.loads(r.read().decode("utf-8") or "{}")
    LAST_CALL["reply"] = f"ntfy đã nhận (id {reply.get('id', '?')})"
    return reply


def main():
    sa = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    test = os.environ.get("NOTIFY_TEST") == "1"
    if not sa or not token:
        log("Chưa cài FIREBASE_SERVICE_ACCOUNT / TELEGRAM_BOT_TOKEN trong GitHub Secrets → bỏ qua cảnh báo.")
        return 0

    tg = Telegram(token)
    try:
        bot = tg.me()
        pub = {"telegramBot": bot.get("username")}
        old = json.loads(NOTIFY_PUBLIC.read_text("utf-8")) if NOTIFY_PUBLIC.exists() else {}
        if old != pub:
            NOTIFY_PUBLIC.write_text(json.dumps(pub, ensure_ascii=False, indent=1) + "\n", "utf-8")
    except Exception as e:  # noqa: BLE001
        log("Không đọc được thông tin bot Telegram (kiểm tra TELEGRAM_BOT_TOKEN):", e)
        return 0

    import firebase_admin
    from firebase_admin import credentials, firestore

    firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
    db = firestore.client()

    m = json.loads(MARKET.read_text("utf-8"))
    tgt, world = m.get("target") or {}, m.get("world") or {}
    values = {"buy": tgt.get("buy"), "sell": tgt.get("sell"), "world": world.get("usdPerOz")}
    asof = {"buy": m.get("priceAsOf"), "sell": m.get("priceAsOf"), "world": world.get("asOf")}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    users = list(db.document("data/users").collections())
    diag = []
    log(f"Người dùng: {len(users)} · giá: thu mua {values['buy']}, bán {values['sell']}, thế giới {values['world']}")
    for col in users:
        uid = col.id
        prof_ref = col.document("profile")
        prof = (prof_ref.get().to_dict() or {})
        settings = (prof_ref.collection("notify").document("settings").get().to_dict() or {})
        state_ref = prof_ref.collection("notify").document("state")
        state = (state_ref.get().to_dict() or {})
        st_alerts = dict(state.get("alerts") or {})
        new_state = {"lastCheckAt": now}
        username = (settings.get("telegramUsername") or "").strip().lstrip("@")
        want_msg = bool(settings.get("telegramOn")) and bool(username)
        topic = (settings.get("ntfyTopic") or "").strip()
        want_call = bool(settings.get("alarmOn")) and bool(re.fullmatch(r"[A-Za-z0-9_-]{12,64}", topic))

        chat_id = state.get("chatId") if state.get("chatFor") == username.lower() else None
        if want_msg and not chat_id:
            chat_id = tg.find_chat(username)
            if chat_id:
                new_state.update({"chatId": chat_id, "chatFor": username.lower()})
                log(f"[{uid[:6]}] Đã liên kết Telegram @{username}")
        errors = []
        sent = {"msg": 0, "alarm": 0}

        def send_msg(text):
            if not want_msg:
                return
            if not chat_id:
                errors.append(f"Chưa liên kết được Telegram: hãy mở bot và gửi /start bằng tài khoản @{username}.")
                return
            try:
                tg.send(chat_id, text)
                sent["msg"] += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"Tin nhắn Telegram lỗi: {e}")

        def place_call(title, text, test=False):
            if not want_call:
                return
            try:
                alarm(topic, title, text, test)
                sent["alarm"] += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"Báo động ntfy lỗi: {e}")

        if test:
            send_msg("✅ <b>Silver Tracker</b>: thử nghiệm thông báo thành công.\n"
                     f"Thu mua Phú Quý {vnd(values['buy'])}, thế giới {usd(values['world'])}.\n<a href=\"{APP_URL}\">Mở app</a>")
            place_call("Silver Tracker: báo động thử", "Nếu máy đang reo, báo động giá bạc đã sẵn sàng. Bấm vào để tắt.", test=True)
            new_state["lastTestAt"] = now

        fired = 0
        for a in prof.get("alerts") or []:
            k = KINDS.get(a.get("kind"))
            aid = a.get("id")
            if not k or not aid:
                continue
            field, direction, label = k
            val, th = values.get(field), a.get("threshold")
            if not a.get("enabled") or val is None or th is None:
                st_alerts[aid] = "off"
                continue
            hit = val >= th if direction > 0 else val <= th
            # "on:<loại>:<mức>" = đã báo THÀNH CÔNG cho đúng mức này. Sửa mức → báo lại.
            # Chưa gửi được qua kênh nào (chưa cài, lỗi) → giữ "off" để lần sau thử lại.
            key = f"on:{a.get('kind')}:{th}"
            if hit and st_alerts.get(aid) != key:
                f = usd if field == "world" else vnd
                word = "đã chạm hoặc vượt" if direction > 0 else "đã chạm hoặc xuống dưới"
                before = sent["msg"] + sent["alarm"]
                send_msg(f"🔔 <b>Cảnh báo giá bạc</b>\n{label}: <b>{f(val)}</b>\n{word} mức bạn đặt {f(th)}.\n"
                         f"Cập nhật {fmt_time(asof[field])}. <a href=\"{APP_URL}\">Mở app</a>")
                if a.get("call"):
                    place_call(f"Cảnh báo giá bạc: {label}", f"{f(val)} {word} mức {f(th)}. Cập nhật {fmt_time(asof[field])}.")
                if sent["msg"] + sent["alarm"] > before:
                    fired += 1
                    st_alerts[aid] = key
                else:
                    st_alerts[aid] = "pending"
                continue
            st_alerts[aid] = key if hit else "off"

        new_state["alerts"] = st_alerts
        new_state["lastError"] = " · ".join(dict.fromkeys(errors)) if errors else ""
        if LAST_CALL.get("reply"):
            new_state["lastCallReply"] = LAST_CALL.pop("reply")[:200]
            new_state["lastCallAt"] = now
        state_ref.set(new_state, merge=True)
        al = prof.get("alerts") or []
        diag.append({
            "user": len(diag) + 1, "alerts": len(al), "enabled": sum(1 for a in al if a.get("enabled")),
            "markedAlarm": sum(1 for a in al if a.get("call")), "fired": fired,
            "telegram": {"on": bool(settings.get("telegramOn")), "hasUsername": bool(username), "linked": bool(chat_id), "sent": sent["msg"]},
            "alarm": {"on": bool(settings.get("alarmOn")), "topicOk": want_call, "sent": sent["alarm"]},
            "alertsState": {k: sum(1 for a in al if (st_alerts.get(a.get("id")) or "off").split(":")[0] == k) for k in ("on", "pending", "off")},
            "errors": [re.sub(r"@\w+", "@***", e)[:200] for e in dict.fromkeys(errors)],
        })
        log(f"[{uid[:6]}] cảnh báo kích hoạt: {fired}{' · lỗi: ' + errors[-1] if errors else ''}")
    summary = {"users": diag, "prices": values}
    old = json.loads(NOTIFY_LOG.read_text("utf-8")) if NOTIFY_LOG.exists() else {}
    if test or {k: v for k, v in old.items() if k != "at"} != summary:
        NOTIFY_LOG.write_text(json.dumps({"at": now, **summary}, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — cảnh báo lỗi không được làm hỏng lần cập nhật giá
        log("CẢNH BÁO: kiểm tra cảnh báo lỗi:", type(e).__name__, e)
        sys.exit(0)
