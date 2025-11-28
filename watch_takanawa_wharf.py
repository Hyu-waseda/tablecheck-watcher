import os
import re
from datetime import datetime
from urllib.parse import urlparse

from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import requests
import httpx

# ==============================
# 非シークレット設定（ここを書き換える）
# ==============================

# 予約ページURL（これも隠したいなら env に移してOK）
RESERVE_URL = "https://www.tablecheck.com/ja/shops/takanawa-wharf/reserve?utm_source=hp"

# 予約したい日付
TARGET_DATE = "2025-12-24"

# 大人の人数
NUM_PEOPLE_ADULT = 2

# 通知を送ってよい時間帯（ローカル時間）
NOTIFY_START_HOUR = 0   # 例: 8
NOTIFY_END_HOUR = 24    # 例: 24

# 予約したい時間帯（スロットの開始時刻）
SLOT_START_HOUR = 11    # 例: 18
SLOT_END_HOUR = 20      # 例: 20

# タイムゾーン
TIMEZONE = "Asia/Tokyo"

# 高輪 WHARF 固有の席カテゴリ
SEAT_CATEGORIES = {
    "window_1st_couple": {
        "label": "窓際一列目カップルシート",
        "service_category": "688ab4fb01b93519106912dd",
    },
    "window_2nd_couple": {
        "label": "窓際二列目カップルシート",
        "service_category": "688ab5618001bc122c53b18f",
    },
    "view_2p_only": {
        "label": "2名専用ビューシート",
        "service_category": "6910dba6b600519f9e0efc07",
    },
}


def within_window(now: datetime, start_h: int, end_h: int) -> bool:
    """start_h <= hour < end_h のとき True"""
    return start_h <= now.hour < end_h


def sec_to_hm(sec: int) -> str:
    """0時起点の秒数を 'HH:MM' に変換"""
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h:02d}:{m:02d}"


def create_session_and_fetch_csrf(reserve_url: str) -> tuple[requests.Session, str]:
    """
    予約ページをGETして、CSRFトークンとセッションCookieを取る。
    Railsの <meta name="csrf-token" content="..."> をパース。
    """
    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "ja,en;q=0.9,en-US;q=0.8",
    }
    resp = session.get(reserve_url, headers=headers, timeout=10)
    resp.raise_for_status()

    m = re.search(
        r'<meta name="csrf-token" content="([^"]+)"',
        resp.text,
    )
    if not m:
        raise RuntimeError("CSRF token not found in reserve page.")
    csrf_token = m.group(1)
    return session, csrf_token


def build_timetable_url(reserve_url: str) -> str:
    """
    予約URLから /ja/shops/{shop-id}/available/timetable を組み立てる。
    例: /ja/shops/takanawa-wharf/reserve -> /ja/shops/takanawa-wharf/available/timetable
    """
    parsed = urlparse(reserve_url)
    parts = parsed.path.split("/")
    try:
        shops_idx = parts.index("shops")
        shop_id = parts[shops_idx + 1]
    except (ValueError, IndexError):
        raise RuntimeError(f"Unexpected reserve URL path: {parsed.path}")
    timetable_path = f"/ja/shops/{shop_id}/available/timetable"
    return f"{parsed.scheme}://{parsed.netloc}{timetable_path}"


def fetch_timetable(
    session: requests.Session,
    timetable_url: str,
    csrf_token: str,
    target_date: str,
    service_category: str,
    num_people_adult: int,
    reserve_url_for_referer: str,
) -> dict:
    """TableCheck の timetable API を叩いてJSONを返す"""
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ja,en;q=0.9,en-US;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
        "Referer": reserve_url_for_referer,
        "X-CSRF-Token": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
    }
    params = {
        "authenticity_token": csrf_token,
        "reservation[num_people_adult]": str(num_people_adult),
        "reservation[service_category]": service_category,
        "reservation[start_date]": target_date,
    }
    resp = session.get(timetable_url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def extract_available_times(
    data: dict,
    target_date: str,
    slot_start_hour: int,
    slot_end_hour: int,
) -> list[int]:
    """
    JSON から、target_date の slot_start_hour〜slot_end_hour の間で
    available=True のスロット秒数を昇順で返す。
    """
    slots_by_date = data.get("data", {}).get("slots", {})
    day_slots = slots_by_date.get(target_date, {})

    start_sec = slot_start_hour * 3600
    end_sec = slot_end_hour * 3600

    result: list[int] = []
    for slot in day_slots.values():
        seconds = slot.get("seconds")
        available = slot.get("available", False)
        if seconds is None:
            continue
        if start_sec <= seconds < end_sec and available:
            result.append(seconds)

    return sorted(result)


def line_push(message: str, token: str, to_user_id: str) -> None:
    """
    LINE Messaging API の pushを使ってメッセージ送信。
    設定が無ければ標準出力に出すだけ。
    """
    if not token or not to_user_id:
        print("[WARN] LINE credentials not set; printing message instead:")
        print(message)
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": to_user_id,
        "messages": [{"type": "text", "text": message}],
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                "https://api.line.me/v2/bot/message/push",
                headers=headers,
                json=payload,
            )
        if resp.status_code >= 300:
            print(f"[WARN] LINE push failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[WARN] LINE push exception: {e}")


def main() -> None:
    load_dotenv()

    # シークレットは .env / GitHub Secrets から読む
    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    line_to_user = os.getenv("LINE_TO_USER_ID", "").strip()

    now_local = datetime.now(ZoneInfo(TIMEZONE))
    # 「通知して良い時間」かどうかで実行可否を決める
    if not within_window(now_local, NOTIFY_START_HOUR, NOTIFY_END_HOUR):
        print(
            f"[{now_local}] skip (outside notify window "
            f"{NOTIFY_START_HOUR}-{NOTIFY_END_HOUR})"
        )
        return

    now_str = now_local.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] start check")

    try:
        session, csrf_token = create_session_and_fetch_csrf(RESERVE_URL)
    except Exception as e:
        print(f"[ERROR] failed to fetch CSRF token: {e}")
        return

    try:
        timetable_url = build_timetable_url(RESERVE_URL)
    except Exception as e:
        print(f"[ERROR] failed to build timetable URL: {e}")
        return

    any_available = False
    lines_for_message = [
        f"【高輪 WHARF】{TARGET_DATE} "
        f"{SLOT_START_HOUR}:00〜{SLOT_END_HOUR}:00 の空き状況",
    ]

    for key, seat in SEAT_CATEGORIES.items():
        label = seat["label"]
        category_id = seat["service_category"]

        try:
            data = fetch_timetable(
                session=session,
                timetable_url=timetable_url,
                csrf_token=csrf_token,
                target_date=TARGET_DATE,
                service_category=category_id,
                num_people_adult=NUM_PEOPLE_ADULT,
                reserve_url_for_referer=RESERVE_URL,
            )
            times_sec = extract_available_times(
                data=data,
                target_date=TARGET_DATE,
                slot_start_hour=SLOT_START_HOUR,
                slot_end_hour=SLOT_END_HOUR,
            )
        except Exception as e:
            print(f"[ERROR] timetable fetch failed [{label}]: {e}")
            continue

        if times_sec:
            any_available = True
            times_str = ", ".join(sec_to_hm(s) for s in times_sec)
            print(f"  - available [{label}]: {times_str}")
            lines_for_message.append(f"- {label}: {times_str}")
        else:
            print(f"  - no availability [{label}]")

    if any_available:
        lines_for_message.append("")
        lines_for_message.append(RESERVE_URL)
        message = "\n".join(lines_for_message)
        line_push(message, token=line_token, to_user_id=line_to_user)
    else:
        print("no slots found; no LINE push")


if __name__ == "__main__":
    main()
