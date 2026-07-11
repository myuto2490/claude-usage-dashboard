# -*- coding: utf-8 -*-
"""
Claude Code 使用状況ダッシュボード

Windows機のローカルに保存された Claude Code のログ (~/.claude) を読み取り、
トークン使用量・推定コスト・活動量・利用上限を詳細に可視化する常駐Webアプリ。

- 集計元 : ~/.claude/projects/**/*.jsonl
           (モデル別トークン・5分/1時間キャッシュ内訳・コスト・セッション・
            時間帯・バージョン・スキル・Web検索/取得・停止理由・APIエラー を集計)
- 上限   : ~/.claude/.credentials.json 経由で 5時間/週間の利用上限をライブ取得

標準ライブラリのみで動作。ブラウザで http://127.0.0.1:8787 を開くと
5秒ごとに自動更新されるダッシュボードが表示される。
"""

import os
import json
import glob
import time
import socket
import datetime
import subprocess
import threading
import webbrowser
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
PORT = int(os.environ.get("AI_USAGE_PORT", "8787"))

# 既定で LAN にも公開し、同じ Wi-Fi のスマホ/他PCから残量を見られるようにする。
# AI_USAGE_LOCAL_ONLY=1 で従来どおり 127.0.0.1 のみに制限。
# ウィンドウ操作 (POST /api/hide 等) は常にこのPC (loopback) からのみ受け付ける。
LOCAL_ONLY = os.environ.get("AI_USAGE_LOCAL_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
BIND_HOST = "127.0.0.1" if LOCAL_ONLY else "0.0.0.0"

APP_TITLE = "Claude Code 使用状況ダッシュボード"
APP_VERSION = "1.0.0"   # 公開リポジトリのタグ (vX.Y.Z) と揃える


def _flag(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _log(msg=""):
    """windowed 実行 (exe) では stdout が無いため安全に握りつぶす。"""
    try:
        import sys
        if sys.stdout is None:
            return
        print(msg)
    except Exception:
        pass

# Claude サブスク利用上限 (5時間/週間) ライブ取得の設定
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_UA = os.environ.get("AI_USAGE_CLAUDE_UA", "claude-code/2.0.1")  # 無いと429多発
LIMITS_TTL = int(os.environ.get("AI_USAGE_LIMITS_TTL", "60"))         # 通常の再取得間隔(秒)
LIMITS_BACKOFF = int(os.environ.get("AI_USAGE_LIMITS_BACKOFF", "300"))  # 429時のバックオフ(秒)

USDJPY_URLS = [
    u.strip() for u in os.environ.get(
        "AI_USAGE_USDJPY_URLS",
        "https://open.er-api.com/v6/latest/USD,https://api.exchangerate-api.com/v4/latest/USD,https://api.frankfurter.app/latest?from=USD&to=JPY",
    ).split(",") if u.strip()
]
USDJPY_CACHE = os.path.join(HOME, ".ai_usage_dashboard", "usdjpy.json")
USD_JPY_FALLBACK = float(os.environ.get("AI_USAGE_USDJPY", "160"))

# Web ツール単価 (USD)。web_search は 1000 リクエストあたり $10。
WEB_SEARCH_COST = float(os.environ.get("AI_USAGE_WEB_SEARCH_COST", "10.0")) / 1000.0


# ---------------------------------------------------------------------------
# 価格表 (per 1M tokens, USD) — claude-api skill の pricing に準拠
#   cache_write(5分TTL) = 入力 * 1.25 / cache_write(1時間TTL) = 入力 * 2.0
#   cache_read = 入力 * 0.10
# ---------------------------------------------------------------------------
def price_for(model: str):
    """(input, output) USD/1M を返す。"""
    m = (model or "").lower()
    if "fable" in m or "mythos" in m:
        return (10.0, 50.0)
    if "opus" in m:
        # Opus 4.5〜4.8 は 5/25。旧 Opus(4.1/4.0/3) は 15/75。
        if any(v in m for v in ("4-8", "4-7", "4-6", "4-5")):
            return (5.0, 25.0)
        return (15.0, 75.0)
    if "sonnet" in m:
        return (3.0, 15.0)
    if "haiku" in m:
        if "4-5" in m:
            return (1.0, 5.0)
        return (0.80, 4.0)
    # 不明なモデルは Sonnet 相当で概算
    return (3.0, 15.0)


def parse_ts(value):
    """ISO8601/UNIX timestamp -> timezone-aware datetime. 失敗時 None。"""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).astimezone()
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return datetime.datetime.fromtimestamp(int(text), datetime.timezone.utc).astimezone()
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 為替 (USD/JPY) — 1日1回取得しキャッシュ
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_rate_state = {"data": None}


def get_usd_jpy():
    today = datetime.date.today().isoformat()
    with _rate_lock:
        cached_state = _rate_state.get("data")
        if cached_state and cached_state.get("date") == today:
            return cached_state

    cached = None
    try:
        with open(USDJPY_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("date") == today and cached.get("rate"):
            with _rate_lock:
                _rate_state["data"] = cached
            return cached
    except Exception:
        cached = None

    result = {"rate": USD_JPY_FALLBACK, "date": today, "source": "AI_USAGE_USDJPY", "fallback": True}
    try:
        last_error = None
        source = None
        rate = None
        for url in USDJPY_URLS:
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ai-usage-dashboard/1.0"}, method="GET")
                with urllib.request.urlopen(req, timeout=4) as r:
                    raw = json.loads(r.read().decode("utf-8", "ignore"))
                rate = float((raw.get("rates") or {}).get("JPY"))
                source = raw.get("provider") or url.split("/")[2]
                break
            except Exception as e:
                last_error = e
        if rate is None:
            raise last_error or RuntimeError("為替レートを取得できませんでした")
        result = {
            "rate": rate, "date": today, "source": source, "fallback": False,
            "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        try:
            os.makedirs(os.path.dirname(USDJPY_CACHE), exist_ok=True)
            with open(USDJPY_CACHE, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            result["warn"] = f"為替キャッシュ保存に失敗: {e}"
    except Exception as e:
        if cached and cached.get("rate"):
            result = dict(cached)
            result["cache_date"] = result.get("date")
            result["date"] = today
            result["fallback"] = True
            result["warn"] = f"為替の更新に失敗したためキャッシュを使用: {e}"
        else:
            result["warn"] = f"為替の更新に失敗したため環境変数/既定値を使用: {e}"

    with _rate_lock:
        _rate_state["data"] = result
    return result


# ---------------------------------------------------------------------------
# ファイル単位キャッシュ (mtime が変わらなければ再解析しない)
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_file_cache = {}   # path -> (mtime, parsed_object)


def cached_parse(path, parser):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _cache_lock:
        hit = _file_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        parsed = parser(path)
    except Exception:
        parsed = None
    with _cache_lock:
        _file_cache[path] = (mtime, parsed)
    return parsed


# ---------------------------------------------------------------------------
# Claude Code ログ解析
# ---------------------------------------------------------------------------
def _parse_claude_file(path):
    """1つの jsonl を解析。assistant 行の詳細を requestId で重複排除して返す。"""
    project = os.path.basename(os.path.dirname(path))
    seen = set()
    rows = []
    git_branches = set()
    session_ids = set()
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or '"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage") or {}
            if not usage:
                continue
            rid = obj.get("requestId") or msg.get("id")
            if rid is not None:
                if rid in seen:
                    continue
                seen.add(rid)

            dt = parse_ts(obj.get("timestamp"))
            date = dt.date().isoformat() if dt else "unknown"
            hour = dt.hour if dt else None
            weekday = dt.weekday() if dt else None  # 0=Mon

            cc = usage.get("cache_creation") or {}
            cw5 = int(cc.get("ephemeral_5m_input_tokens", 0) or 0)
            cw1 = int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
            cw_total = int(usage.get("cache_creation_input_tokens", 0) or 0)
            if not (cw5 or cw1) and cw_total:
                cw5 = cw_total  # 内訳が無い場合は5分TTL扱い
            stu = usage.get("server_tool_use") or {}
            web_search = int(stu.get("web_search_requests", 0) or 0)
            web_fetch = int(stu.get("web_fetch_requests", 0) or 0)

            sid = obj.get("sessionId")
            if sid:
                session_ids.add(sid)
            gb = obj.get("gitBranch")
            if gb:
                git_branches.add(gb)

            rows.append({
                "date": date,
                "hour": hour,
                "weekday": weekday,
                "model": msg.get("model") or "unknown",
                "in": int(usage.get("input_tokens", 0) or 0),
                "out": int(usage.get("output_tokens", 0) or 0),
                "cw5": cw5,
                "cw1": cw1,
                "cr": int(usage.get("cache_read_input_tokens", 0) or 0),
                "web_search": web_search,
                "web_fetch": web_fetch,
                "stop": msg.get("stop_reason") or "unknown",
                "version": obj.get("version") or "unknown",
                "entrypoint": obj.get("entrypoint") or "unknown",
                "service_tier": usage.get("service_tier") or "unknown",
                "skill": obj.get("attributionSkill"),
                "error": bool(obj.get("isApiErrorMessage")),
                "session": sid,
            })
    return {"project": project, "rows": rows, "branches": sorted(git_branches), "sessions": sorted(session_ids)}


def _row_cost(r):
    p_in, p_out = price_for(r["model"])
    cost = (
        r["in"] * p_in
        + r["out"] * p_out
        + r["cw5"] * p_in * 1.25
        + r["cw1"] * p_in * 2.0
        + r["cr"] * p_in * 0.10
    ) / 1e6
    cost += r["web_search"] * WEB_SEARCH_COST
    return cost


def collect_claude():
    base = os.path.join(HOME, ".claude", "projects")
    files = glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)

    def blank():
        return {"in": 0, "out": 0, "cw5": 0, "cw1": 0, "cr": 0, "cost": 0.0, "calls": 0}

    totals = {**blank(), "web_search": 0, "web_fetch": 0, "errors": 0}
    by_model, by_date, by_project = {}, {}, {}
    by_hour = {h: 0 for h in range(24)}
    by_weekday = {d: 0 for d in range(7)}
    by_version, by_skill, by_stop, by_tier = {}, {}, {}, {}
    all_sessions = set()

    def add(d, key, r, cost):
        e = d.setdefault(key, blank())
        e["in"] += r["in"]; e["out"] += r["out"]
        e["cw5"] += r["cw5"]; e["cw1"] += r["cw1"]; e["cr"] += r["cr"]
        e["cost"] += cost; e["calls"] += 1

    for path in files:
        parsed = cached_parse(path, _parse_claude_file)
        if not parsed:
            continue
        proj = parsed["project"]
        all_sessions.update(parsed.get("sessions", []))
        proj_entry = by_project.setdefault(proj, {**blank(), "branches": set(), "sessions": set()})
        proj_entry["branches"].update(parsed.get("branches", []))
        proj_entry["sessions"].update(parsed.get("sessions", []))
        for r in parsed["rows"]:
            cost = _row_cost(r)
            add(by_model, r["model"], r, cost)
            add(by_date, r["date"], r, cost)
            # project 集計 (branches/sessions は別管理)
            pe = proj_entry
            pe["in"] += r["in"]; pe["out"] += r["out"]
            pe["cw5"] += r["cw5"]; pe["cw1"] += r["cw1"]; pe["cr"] += r["cr"]
            pe["cost"] += cost; pe["calls"] += 1

            totals["in"] += r["in"]; totals["out"] += r["out"]
            totals["cw5"] += r["cw5"]; totals["cw1"] += r["cw1"]; totals["cr"] += r["cr"]
            totals["cost"] += cost; totals["calls"] += 1
            totals["web_search"] += r["web_search"]; totals["web_fetch"] += r["web_fetch"]
            if r["error"]:
                totals["errors"] += 1
            if r["hour"] is not None:
                by_hour[r["hour"]] += 1
            if r["weekday"] is not None:
                by_weekday[r["weekday"]] += 1
            by_version[r["version"]] = by_version.get(r["version"], 0) + 1
            by_stop[r["stop"]] = by_stop.get(r["stop"], 0) + 1
            by_tier[r["service_tier"]] = by_tier.get(r["service_tier"], 0) + 1
            if r["skill"]:
                by_skill[r["skill"]] = by_skill.get(r["skill"], 0) + 1

    def as_list(d):
        out = []
        for k, v in d.items():
            row = {"key": k}
            row.update(v)
            out.append(row)
        return out

    proj_list = []
    for k, v in by_project.items():
        row = {"key": k}
        row.update({kk: vv for kk, vv in v.items() if kk not in ("branches", "sessions")})
        row["branches"] = sorted(v["branches"])
        row["sessions"] = len(v["sessions"])
        proj_list.append(row)
    proj_list.sort(key=lambda r: -r["cost"])

    today = datetime.date.today().isoformat()
    today_stat = by_date.get(today, blank())

    # キャッシュ効率: cache_read / (入力 + cache_read)
    cr = totals["cr"]
    cache_eff = (cr / (totals["in"] + cr) * 100.0) if (totals["in"] + cr) else 0.0

    return {
        "totals": totals,
        "today": today_stat,
        "sessions": len(all_sessions),
        "cache_efficiency": cache_eff,
        "by_model": sorted(as_list(by_model), key=lambda r: -r["cost"]),
        "by_date": sorted(as_list(by_date), key=lambda r: r["key"]),
        "by_project": proj_list,
        "by_hour": [{"key": h, "count": by_hour[h]} for h in range(24)],
        "by_weekday": [{"key": d, "count": by_weekday[d]} for d in range(7)],
        "by_version": sorted([{"key": k, "count": v} for k, v in by_version.items()], key=lambda r: -r["count"]),
        "by_skill": sorted([{"key": k, "count": v} for k, v in by_skill.items()], key=lambda r: -r["count"]),
        "by_stop": sorted([{"key": k, "count": v} for k, v in by_stop.items()], key=lambda r: -r["count"]),
        "by_tier": sorted([{"key": k, "count": v} for k, v in by_tier.items()], key=lambda r: -r["count"]),
        "files": len(files),
    }


# ---------------------------------------------------------------------------
# Claude サブスク利用上限 (5時間 / 週間) — Anthropic からライブ取得
# ---------------------------------------------------------------------------
_limits_lock = threading.Lock()
_limits_state = {"last_fetch": 0.0, "next_ok": 0.0, "data": None}


CREDENTIALS_PATH = os.path.join(HOME, ".claude", ".credentials.json")
# Claude Code の公開 OAuth クライアント ID (トークン更新に使用)
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_refresh_lock = threading.Lock()
_refresh_state = {"last_attempt": 0.0, "last_result": None}


def _refresh_oauth_token(creds, oauth):
    """期限切れが近いアクセストークンをリフレッシュトークンで更新する。

    このPCで Claude Code を長期間使わないとトークンが更新されず残量取得が
    止まるため (スマホのみで作業するケース)、ダッシュボード自身が更新する。
    更新後は Claude Code と同じ形式で .credentials.json に原子的に書き戻す
    (リフレッシュトークンはローテーションされるため、書き戻さないと
    Claude Code 側のログインが切れる)。
    """
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        return None

    now = time.monotonic()
    if now - _refresh_state["last_attempt"] < 60:   # 失敗の連打を防ぐ
        return None
    _refresh_state["last_attempt"] = now

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # 既定の Python UA は Cloudflare (error 1010) に弾かれる
            "User-Agent": CLAUDE_UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        res = json.loads(r.read().decode("utf-8", "ignore"))

    access = res.get("access_token")
    if not access:
        return None
    oauth = dict(oauth)
    oauth["accessToken"] = access
    if res.get("refresh_token"):
        oauth["refreshToken"] = res["refresh_token"]
    if res.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(res["expires_in"]) * 1000

    # Claude Code が読み込む形式のまま原子的に書き戻す
    creds = dict(creds)
    creds["claudeAiOauth"] = oauth
    tmp = CREDENTIALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False)
    os.replace(tmp, CREDENTIALS_PATH)
    _refresh_state["last_result"] = datetime.datetime.now().isoformat(timespec="seconds")
    return oauth


def _read_oauth_token():
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            creds = json.load(f) or {}
        oauth = creds.get("claudeAiOauth") or {}

        # 期限切れ (10分前から) なら自分でリフレッシュする。
        # 失敗しても手元のトークンで続行し、従来どおり 401 側で通知する。
        expires_at = oauth.get("expiresAt")
        if expires_at and (expires_at / 1000.0 - time.time()) < 600:
            with _refresh_lock:
                try:
                    refreshed = _refresh_oauth_token(creds, oauth)
                    if refreshed:
                        oauth = refreshed
                except Exception:
                    pass

        return {
            "token": oauth.get("accessToken"),
            "expires_at": oauth.get("expiresAt"),
            "subscription": oauth.get("subscriptionType"),
            "tier": oauth.get("rateLimitTier"),
            "token_refreshed_at": _refresh_state["last_result"],
        }
    except FileNotFoundError:
        return {"error": "Claude の認証情報が見つかりません (未ログイン)"}
    except Exception as e:
        return {"error": f"認証情報の読み取りに失敗: {e}"}


def _shape_usage(raw, meta):
    """API レスポンス -> ダッシュボード用に整形。"""
    def win(w):
        if not isinstance(w, dict):
            return None
        util = w.get("utilization")
        if util is None:
            return None
        util = float(util)
        return {"utilization": util, "remaining": max(0.0, 100.0 - util), "resets_at": w.get("resets_at")}

    limits = []
    for it in (raw.get("limits") or []):
        if not isinstance(it, dict):
            continue
        scope = it.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name") if isinstance(scope, dict) else None
        limits.append({
            "kind": it.get("kind"),
            "group": it.get("group"),
            "percent": it.get("percent"),
            "remaining": (100 - it.get("percent")) if isinstance(it.get("percent"), (int, float)) else None,
            "severity": it.get("severity"),
            "resets_at": it.get("resets_at"),
            "model": model,
            "is_active": it.get("is_active"),
        })

    return {
        "available": True,
        "subscription": meta.get("subscription"),
        "tier": meta.get("tier"),
        "five_hour": win(raw.get("five_hour")),
        "seven_day": win(raw.get("seven_day")),
        "seven_day_opus": win(raw.get("seven_day_opus")),
        "seven_day_sonnet": win(raw.get("seven_day_sonnet")),
        "limits": limits,
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def collect_limits():
    now = time.monotonic()
    with _limits_lock:
        cached = _limits_state["data"]
        if cached and now < _limits_state["next_ok"]:
            out = dict(cached)
            out["stale"] = (now - _limits_state["last_fetch"]) > LIMITS_TTL + 5
            return out

    meta = _read_oauth_token()
    if meta.get("error") or not meta.get("token"):
        result = {"available": False, "reason": meta.get("error") or "アクセストークンがありません"}
        with _limits_lock:
            if _limits_state["data"] is not None:
                _limits_state["next_ok"] = now + 30
                out = dict(_limits_state["data"]); out["warn"] = result["reason"]; out["stale"] = True
                return out
            _limits_state["next_ok"] = now + 30
        return result

    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {meta['token']}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": CLAUDE_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read().decode("utf-8", "ignore"))
        shaped = _shape_usage(raw, meta)
        with _limits_lock:
            _limits_state["data"] = shaped
            _limits_state["last_fetch"] = now
            _limits_state["next_ok"] = now + LIMITS_TTL
        out = dict(shaped); out["stale"] = False
        return out
    except urllib.error.HTTPError as e:
        code = e.code
        reason = f"HTTP {code}"
        if code == 401:
            reason = "トークン期限切れ/無効 — 自動更新を試行中 (失敗が続く場合は Claude Code で再ログイン)"
            # 期限判定をすり抜けた無効トークン。次回すぐ再読込+リフレッシュを試す
            try:
                with open(CREDENTIALS_PATH, encoding="utf-8") as f:
                    creds = json.load(f) or {}
                with _refresh_lock:
                    _refresh_oauth_token(creds, creds.get("claudeAiOauth") or {})
            except Exception:
                pass
        elif code == 429:
            reason = "レート制限 (429) — しばらく待って再取得します"
        backoff = LIMITS_BACKOFF if code == 429 else 60
        with _limits_lock:
            _limits_state["next_ok"] = now + backoff
            if _limits_state["data"] is not None:
                out = dict(_limits_state["data"]); out["warn"] = reason; out["stale"] = True
                return out
        return {"available": False, "reason": reason}
    except Exception as e:
        with _limits_lock:
            _limits_state["next_ok"] = now + 60
            if _limits_state["data"] is not None:
                out = dict(_limits_state["data"]); out["warn"] = f"取得エラー: {e}"; out["stale"] = True
                return out
        return {"available": False, "reason": f"取得エラー: {e}"}


# ---------------------------------------------------------------------------
# 日次履歴の永続化 (カレンダー用)
#   ログが削除・ローテートされても過去の使用量/コストが残るよう、
#   日付ごとの集計を JSON に追記保存する。
#   ログに存在する日付は再計算値で上書き、存在しない日付は保存値を維持。
# ---------------------------------------------------------------------------
HISTORY_PATH = os.path.join(HOME, ".ai_usage_dashboard", "history.json")
_hist_lock = threading.Lock()
_hist_cache = {"data": None, "sig": None}


def _load_history():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def update_history(by_date, rate):
    """by_date (ログ再計算値) を履歴にマージして保存し、全履歴を返す。"""
    with _hist_lock:
        hist = _hist_cache["data"]
        if hist is None:
            hist = _load_history()

        today = datetime.date.today().isoformat()
        for row in by_date:
            d = row.get("key")
            if not d or d == "unknown":
                continue
            prev = hist.get(d) or {}
            # 過去日は初回記録時のレートを維持し、当日は最新レートで換算する
            use_rate = rate if (d == today or not prev.get("rate")) else float(prev["rate"])
            hist[d] = {
                "cost_usd": round(row["cost"], 6),
                "cost_jpy": int(round(row["cost"] * use_rate)),
                "rate": use_rate,
                "calls": row["calls"],
                "in": row["in"],
                "out": row["out"],
                "cr": row["cr"],
                "cw5": row["cw5"],
                "cw1": row["cw1"],
                "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }

        sig = json.dumps(
            {k: (v.get("cost_usd"), v.get("calls"), v.get("cost_jpy")) for k, v in hist.items()},
            sort_keys=True,
        )
        if sig != _hist_cache["sig"]:
            try:
                os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
                tmp = HISTORY_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(hist, f, ensure_ascii=False, indent=1, sort_keys=True)
                os.replace(tmp, HISTORY_PATH)  # 途中終了で壊れないよう原子的に置換
                _hist_cache["sig"] = sig
            except Exception:
                pass

        _hist_cache["data"] = hist
        return dict(hist)


# ---------------------------------------------------------------------------
# LAN アクセス (スマホ/他PC から残量を見る)
# ---------------------------------------------------------------------------
_lan_cache = {"ts": 0.0, "ips": []}


def _lan_ips():
    """この PC の LAN 側 IP (例 192.168.x.x)。60秒キャッシュ。"""
    now = time.monotonic()
    if now - _lan_cache["ts"] < 60:
        return _lan_cache["ips"]
    ips = []
    try:
        # 実際には送信しない UDP connect でルーティング上の自 IP を得る定石
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    _lan_cache["ts"] = now
    _lan_cache["ips"] = ips
    return ips


def _lan_info():
    if LOCAL_ONLY:
        return {"enabled": False, "urls": []}
    return {"enabled": True,
            "urls": [f"http://{ip}:{PORT}/status" for ip in _lan_ips()]}


# ---------------------------------------------------------------------------
# 集約
# ---------------------------------------------------------------------------
def build_payload():
    claude = collect_claude()
    limits = collect_limits()
    usd_jpy = get_usd_jpy()
    calendar = update_history(claude["by_date"], usd_jpy["rate"])
    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
        "usd_jpy": usd_jpy["rate"],
        "usd_jpy_meta": usd_jpy,
        "limits": limits,
        "claude": claude,
        "calendar": calendar,
        "history_path": HISTORY_PATH,
        "lan": _lan_info(),
    }


# ---------------------------------------------------------------------------
# ウィンドウ操作 (隠す/開く/終了)
#
#   本体ウィンドウと常時表示バーの両方に pywebview の js_api を割り当てると、
#   JS からのブリッジ呼び出し (window.pywebview.api.*) が内部でデッドロック
#   することを確認したため (2ウィンドウ + js_api + 実際の呼び出しの組み合わせ
#   でのみ再現)、js_api は使わずページの JS から直接この HTTP サーバへ
#   POST するだけにしている。pywebview のウィンドウ操作メソッドはスレッド
#   セーフなので、サーバのワーカースレッドから呼んでも問題ない
#   (自動整列/アイコン適用でも同じパターンを使用済み)。
# ---------------------------------------------------------------------------
_windows = {"main": None, "overlay": None, "quitting": False}


def _window_action_hide():
    m = _windows.get("main")
    if m is not None:
        m.hide()


def _window_action_show():
    m = _windows.get("main")
    if m is not None:
        m.show()
        m.restore()


def _window_action_quit():
    o = _windows.get("overlay")
    m = _windows.get("main")
    # closing フック (X=隠す) を通過させ、実際に閉じられるようにする
    _windows["quitting"] = True
    if m is not None:
        try:
            # 常時表示バー側で2度押し確認済みのため、本体のネイティブ終了
            # 確認 (confirm_close) を二重に出さないよう、ここで無効化する。
            m.confirm_close = False
        except Exception:
            pass
    if o is not None:
        try:
            o.destroy()
        except Exception:
            pass
    if m is not None:
        try:
            m.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP サーバ
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def handle_one_request(self):
        # ウィンドウを閉じた瞬間に接続が切れるのは正常。トレースバックを出さない。
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/data":
            try:
                payload = json.dumps(build_payload(), ensure_ascii=False)
                self._send(200, payload, "application/json; charset=utf-8")
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                raise
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json; charset=utf-8")
        elif path in ("/", "/index.html"):
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/overlay":
            self._send(200, OVERLAY_HTML, "text/html; charset=utf-8")
        elif path == "/status":
            self._send(200, STATUS_HTML, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        # ウィンドウ操作はこの PC (loopback) からのみ。LAN のスマホ等からは
        # 閲覧 (GET) だけを許可し、隠す/終了などの操作はさせない。
        client = (self.client_address or ("",))[0]
        if client not in ("127.0.0.1", "::1"):
            self._send(403, json.dumps({"error": "forbidden"}), "application/json; charset=utf-8")
            return
        path = self.path.split("?", 1)[0]
        actions = {
            "/api/hide": _window_action_hide,
            "/api/show": _window_action_show,
            "/api/quit": _window_action_quit,
        }
        fn = actions.get(path)
        if fn is None:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            fn()
            self._send(200, "{}", "application/json; charset=utf-8")
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            raise
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}), "application/json; charset=utf-8")


# ---------------------------------------------------------------------------
# 画面上辺に常時表示するオーバーレイ (枠なし・最前面)
# ---------------------------------------------------------------------------
OVERLAY_HTML = r"""<!doctype html>
<html lang="ja">
<head><meta charset="utf-8">
<style>
  :root{
    --ink:#f5f5f7; --ink2:rgba(235,235,245,.62); --ink3:rgba(235,235,245,.34);
    --hair:rgba(255,255,255,.14);
    --good:#30d158; --warn:#ff9f0a; --crit:#ff453a;
  }
  html{margin:0;overflow:hidden}
  body{margin:0;overflow:hidden;
    /* 内容ぴったりに詰める (余白を作らない)。窓の縦横は起動後に実測して合わせる */
    display:inline-flex;align-items:center;padding:7px 4px 7px 6px;
    font-family:-apple-system,"SF Pro Text","Segoe UI Variable Text","Segoe UI",Meiryo,system-ui,sans-serif;
    font-size:13px;color:var(--ink);
    background:linear-gradient(rgba(255,255,255,.07),rgba(255,255,255,0) 46%),rgba(28,28,30,.92);
    border:1px solid var(--hair);border-top:none;border-radius:0 0 16px 16px;
    box-shadow:0 10px 30px rgba(0,0,0,.38);
    cursor:move;-webkit-user-select:none;user-select:none;
  }
  .grp{display:flex;align-items:center;gap:7px;padding:0 11px;white-space:nowrap}
  .sep{width:1px;height:20px;background:var(--hair);flex:none}
  .ring{width:24px;height:24px;transform:rotate(-90deg);flex:none}
  .ring circle{fill:none;stroke-width:2.6}
  .ring .fill{stroke-linecap:round;stroke-dasharray:59.69;stroke-dashoffset:59.69;
              transition:stroke-dashoffset .6s ease,stroke .3s ease}
  .txt{display:flex;flex-direction:column;gap:1px;line-height:1}
  .lbl{font-size:9.5px;font-weight:600;color:var(--ink2);letter-spacing:.03em}
  .rst{font-weight:400;color:var(--ink3);margin-left:2px}
  .val{font-size:15px;font-weight:700;letter-spacing:-.01em}
  .val small{font-size:10px;font-weight:600;color:var(--ink2);margin-left:1px}
  #err{color:var(--crit);font-size:10.5px;padding:0 8px}
  .stamp{font-size:9px;color:var(--ink3);font-variant-numeric:tabular-nums;
         padding:0 8px 0 6px;letter-spacing:.02em}
  .ibtn{width:24px;height:24px;flex:none;border:none;background:transparent;color:var(--ink2);
        border-radius:7px;display:flex;align-items:center;justify-content:center;
        cursor:pointer;padding:0;margin:0 2px}
  .ibtn:hover{background:rgba(255,255,255,.14);color:var(--ink)}
  .ibtn:active{transform:scale(.92)}
  .ibtn svg{width:13px;height:13px;pointer-events:none}
  /* 終了ボタンの2度押し確認 (ネイティブ confirm はバーの背後に隠れて操作不能) */
  .ibtn.armed{background:var(--crit);color:#fff;animation:pulse 1s ease infinite}
  .ibtn.armed:hover{background:var(--crit)}
  @keyframes pulse{50%{opacity:.72}}
</style></head>
<body class="pywebview-drag-region">
  <div class="grp">
    <svg class="ring" viewBox="0 0 24 24">
      <circle class="track" id="fh-t" cx="12" cy="12" r="9.5"/>
      <circle class="fill"  id="fh-f" cx="12" cy="12" r="9.5"/>
    </svg>
    <div class="txt"><span class="lbl">5h<span class="rst" id="fhr"></span></span><span class="val" id="fh">–</span></div>
  </div>
  <div class="sep"></div>
  <div class="grp">
    <svg class="ring" viewBox="0 0 24 24">
      <circle class="track" id="wk-t" cx="12" cy="12" r="9.5"/>
      <circle class="fill"  id="wk-f" cx="12" cy="12" r="9.5"/>
    </svg>
    <div class="txt"><span class="lbl">Weekly<span class="rst" id="wkr"></span></span><span class="val" id="wk">–</span></div>
  </div>
  <div class="sep"></div>
  <div class="grp"><div class="txt"><span class="lbl">Today</span><span class="val" id="td">–</span></div></div>
  <div class="sep"></div>
  <div class="grp"><div class="txt"><span class="lbl">Total<span class="rst" id="ttm"></span></span><span class="val" id="tt">–</span></div></div>
  <span id="err"></span>
  <span class="stamp" id="st"></span>
  <button class="ibtn" id="btnShow" title="ダッシュボードを開く">
    <svg viewBox="0 0 24 24" fill="none"><rect x="4" y="5.5" width="16" height="13" rx="2.6"
      stroke="currentColor" stroke-width="1.6"/><line x1="4" y1="9.3" x2="20" y2="9.3"
      stroke="currentColor" stroke-width="1.6"/></svg>
  </button>
  <button class="ibtn" id="btnQuit" title="終了">
    <svg viewBox="0 0 24 24" fill="none"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"
      stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
  </button>
<script>
const CIRC = 59.69; // 2πr (r=9.5)
const yen = n => "¥" + Math.round(n||0).toLocaleString();
function tone(rem){ if(rem==null) return "#8e8e93"; if(rem<=10) return "var(--crit)"; if(rem<=25) return "var(--warn)"; return "var(--good)"; }
function toneRaw(rem){ if(rem==null) return "142,142,147"; if(rem<=10) return "255,69,58"; if(rem<=25) return "255,159,10"; return "48,209,88"; }
function cd(iso){
  if(!iso) return "";
  const t = new Date(iso).getTime(); if(isNaN(t)) return "";
  let s = Math.max(0, Math.round((t-Date.now())/1000));
  const d=Math.floor(s/86400); s-=d*86400;
  const h=Math.floor(s/3600); s-=h*3600; const m=Math.floor(s/60);
  if(d>0) return ` · ${d}d${h}h`;
  if(h>0) return ` · ${h}h${m}m`;
  return ` · ${m}m`;
}
function setWin(id, w){
  const v = document.getElementById(id), r = document.getElementById(id+"r");
  const track = document.getElementById(id+"-t"), fill = document.getElementById(id+"-f");
  if(!w){
    v.textContent = "–"; r.textContent = "";
    fill.style.strokeDashoffset = CIRC;
    track.style.stroke = "rgba(142,142,147,.25)";
    return;
  }
  const rem = w.remaining;
  v.innerHTML = rem.toFixed(0) + '<small>%</small>';
  r.textContent = cd(w.resets_at);
  // リングの塗りが残量と状態(色)を運ぶ。トラックは同色の淡いステップ。
  fill.style.stroke = tone(rem);
  fill.style.strokeDashoffset = (CIRC * (1 - Math.min(100, Math.max(0, rem)) / 100)).toFixed(2);
  track.style.stroke = `rgba(${toneRaw(rem)},.22)`;
}
// 当月ぶんの合計 (円)。履歴に保存済みの日次金額を今月だけ足す = 月初にリセット
function monthTotalYen(p){
  const now = new Date();
  const pre = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-`;
  let sum = 0;
  for(const [d, e] of Object.entries(p.calendar || {})){
    if(d.startsWith(pre)) sum += e.cost_jpy || 0;
  }
  return sum;
}
async function tick(){
  try{
    const p = await (await fetch("/api/data",{cache:"no-store"})).json();
    const L = p.limits || {}, rate = p.usd_jpy || 0, c = p.claude;
    document.getElementById("err").textContent = L.available ? "" : "上限取得不可";
    setWin("fh", L.available ? L.five_hour : null);
    setWin("wk", L.available ? L.seven_day : null);
    document.getElementById("td").textContent = yen((c.today.cost||0)*rate);
    document.getElementById("tt").textContent = yen(monthTotalYen(p));
    document.getElementById("ttm").textContent = ` · ${new Date().toLocaleString("en-US",{month:"short"})}`;
    document.getElementById("st").textContent = (p.generated_at||"").slice(11);
  }catch(e){
    document.getElementById("err").textContent = "接続不可";
  }
}
tick(); setInterval(tick, 5000);

// ボタン上での mousedown はウィンドウのドラッグに使われないようにする
["btnShow", "btnQuit"].forEach(id => {
  document.getElementById(id).addEventListener("mousedown", e => e.stopPropagation());
});
document.getElementById("btnShow").addEventListener("click", () => {
  fetch("/api/show", {method:"POST"}).catch(()=>{});
});
// 終了は2度押しで確定する。confirm() はこの最前面バーの背後にダイアログが
// 隠れて操作不能になり、JS ごと固まるため使わない (実測)。
let quitArm = 0, quitTimer = null;
const btnQuit = document.getElementById("btnQuit");
btnQuit.addEventListener("click", () => {
  const now = Date.now();
  if(now - quitArm < 3000){
    fetch("/api/quit", {method:"POST"}).catch(()=>{});
    return;
  }
  quitArm = now;
  btnQuit.classList.add("armed");
  btnQuit.title = "もう一度押すと終了します";
  clearTimeout(quitTimer);
  quitTimer = setTimeout(() => {
    btnQuit.classList.remove("armed");
    btnQuit.title = "終了";
    quitArm = 0;
  }, 3000);
});
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# スマホ/他PC 向けの残量ページ (/status) — 同じネットワークから閲覧できる
# ---------------------------------------------------------------------------
STATUS_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Claude 残量</title>
<style>
  :root{
    --bg:#000; --panel:#1c1c1e; --line:rgba(255,255,255,.12);
    --txt:#fff; --muted:rgba(235,235,245,.6); --faint:rgba(235,235,245,.3);
    --good:#30d158; --warn:#ff9f0a; --crit:#ff453a;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f2f2f7; --panel:#fff; --line:rgba(60,60,67,.18);
           --txt:#000; --muted:rgba(60,60,67,.6); --faint:rgba(60,60,67,.3);
           --good:#34c759; --warn:#ff9500; --crit:#ff3b30; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);min-height:100vh;
    font-family:-apple-system,"SF Pro Text","Segoe UI",Meiryo,system-ui,sans-serif;
    -webkit-font-smoothing:antialiased;padding:max(20px,env(safe-area-inset-top)) 18px 32px}
  h1{font-size:17px;font-weight:600;margin:6px 2px 18px;letter-spacing:-.01em;
     display:flex;align-items:baseline;gap:10px}
  h1 small{font-size:11px;color:var(--faint);font-weight:400;font-variant-numeric:tabular-nums}
  .rings{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .ringcard{background:var(--panel);border-radius:18px;padding:18px 14px;text-align:center}
  .ringcard svg{width:104px;height:104px;transform:rotate(-90deg)}
  .ringcard circle{fill:none;stroke-width:9}
  .ringcard .fill{stroke-linecap:round;transition:stroke-dashoffset .6s ease}
  .ringwrap{position:relative;display:inline-block}
  .pct{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .pct b{font-size:24px;font-weight:700;letter-spacing:-.02em}
  .pct b small{font-size:12px;color:var(--muted);font-weight:600}
  .rname{margin-top:10px;font-size:13px;font-weight:600}
  .rreset{font-size:11px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
  .money{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
  .mcard{background:var(--panel);border-radius:18px;padding:16px 18px}
  .mcard .l{font-size:12px;color:var(--muted);font-weight:500}
  .mcard .v{font-size:24px;font-weight:700;margin-top:4px;letter-spacing:-.02em}
  .note{font-size:11px;color:var(--faint);margin-top:18px;text-align:center;line-height:1.6}
  .err{color:var(--crit);text-align:center;margin-top:16px;font-size:13px}
</style>
</head>
<body>
<h1>Claude 残量 <small id="st">—</small></h1>
<div class="rings">
  <div class="ringcard">
    <div class="ringwrap">
      <svg viewBox="0 0 104 104">
        <circle class="track" id="fh-t" cx="52" cy="52" r="44"/>
        <circle class="fill"  id="fh-f" cx="52" cy="52" r="44"/>
      </svg>
      <div class="pct"><b id="fh">–</b></div>
    </div>
    <div class="rname">5時間枠</div>
    <div class="rreset" id="fhr">&nbsp;</div>
  </div>
  <div class="ringcard">
    <div class="ringwrap">
      <svg viewBox="0 0 104 104">
        <circle class="track" id="wk-t" cx="52" cy="52" r="44"/>
        <circle class="fill"  id="wk-f" cx="52" cy="52" r="44"/>
      </svg>
      <div class="pct"><b id="wk">–</b></div>
    </div>
    <div class="rname">週間</div>
    <div class="rreset" id="wkr">&nbsp;</div>
  </div>
</div>
<div class="money">
  <div class="mcard"><div class="l">Today</div><div class="v" id="td">–</div></div>
  <div class="mcard"><div class="l" id="ttl">Total</div><div class="v" id="tt">–</div></div>
</div>
<div class="err" id="err"></div>
<div class="note">Anthropic アカウント全体の実値 — スマホや他のPCでの Claude 使用分も反映されます。<br>15秒ごとに自動更新。ホーム画面に追加するとアプリのように使えます。</div>
<script>
const CIRC = 2 * Math.PI * 44;
const yen = n => "¥" + Math.round(n||0).toLocaleString();
function tone(rem){ if(rem==null) return "#8e8e93"; if(rem<=10) return "var(--crit)"; if(rem<=25) return "var(--warn)"; return "var(--good)"; }
function toneRaw(rem){ if(rem==null) return "142,142,147"; if(rem<=10) return "255,69,58"; if(rem<=25) return "255,159,10"; return "48,209,88"; }
function cd(iso){
  if(!iso) return "";
  const t = new Date(iso).getTime(); if(isNaN(t)) return "";
  let s = Math.max(0, Math.round((t-Date.now())/1000));
  const d=Math.floor(s/86400); s-=d*86400;
  const h=Math.floor(s/3600); s-=h*3600; const m=Math.floor(s/60);
  if(d>0) return `リセットまで ${d}日${h}時間`;
  if(h>0) return `リセットまで ${h}時間${m}分`;
  return `リセットまで ${m}分`;
}
function setRing(id, w){
  const v=document.getElementById(id), r=document.getElementById(id+"r");
  const track=document.getElementById(id+"-t"), fill=document.getElementById(id+"-f");
  fill.style.strokeDasharray = CIRC;
  if(!w){ v.textContent="–"; r.innerHTML="&nbsp;";
    fill.style.strokeDashoffset=CIRC; track.style.stroke="rgba(142,142,147,.25)"; return; }
  const rem = w.remaining;
  v.innerHTML = rem.toFixed(0) + '<small>%</small>';
  r.textContent = cd(w.resets_at) || " ";
  fill.style.stroke = tone(rem);
  fill.style.strokeDashoffset = (CIRC * (1 - Math.min(100,Math.max(0,rem))/100)).toFixed(2);
  track.style.stroke = `rgba(${toneRaw(rem)},.22)`;
}
function monthTotalYen(p){
  const n=new Date();
  const pre=`${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,"0")}-`;
  let s=0;
  for(const [d,e] of Object.entries(p.calendar||{})) if(d.startsWith(pre)) s+=e.cost_jpy||0;
  return s;
}
async function tick(){
  try{
    const p = await (await fetch("/api/data",{cache:"no-store"})).json();
    const L=p.limits||{}, c=p.claude, rate=p.usd_jpy||0;
    document.getElementById("err").textContent = L.available ? "" : ("残量を取得できません: "+(L.reason||""));
    setRing("fh", L.available ? L.five_hour : null);
    setRing("wk", L.available ? L.seven_day : null);
    document.getElementById("td").textContent = yen((c.today.cost||0)*rate);
    document.getElementById("tt").textContent = yen(monthTotalYen(p));
    document.getElementById("ttl").textContent = `Total · ${new Date().getMonth()+1}月`;
    document.getElementById("st").textContent = (p.generated_at||"").slice(11,16);
  }catch(e){
    document.getElementById("err").textContent = "接続できません (PC側のアプリが起動しているか確認)";
  }
}
tick(); setInterval(tick, 15000);
</script>
</body></html>
"""


INDEX_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code 使用状況ダッシュボード</title>
<style>
  /* Apple HIG に倣ったセマンティックカラー (ダーク既定 / ライトは下で上書き) */
  :root{
    --bg:#000000;            /* systemBackground */
    --panel:#1c1c1e;         /* secondarySystemBackground */
    --panel2:#2c2c2e;        /* tertiarySystemBackground */
    --line:rgba(255,255,255,.12);   /* separator */
    --txt:#ffffff;                   /* label */
    --muted:rgba(235,235,245,.6);    /* secondaryLabel */
    --faint:rgba(235,235,245,.3);    /* tertiaryLabel */
    --accent:#0a84ff;        /* systemBlue (dark) */
    --claude:#d68a5c;
    --good:#30d158; --warn:#ff9f0a; --crit:#ff453a;  /* systemGreen/Orange/Red */
    --fill:rgba(120,120,128,.24);    /* secondarySystemFill */
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f2f2f7; --panel:#ffffff; --panel2:#f2f2f7;
           --line:rgba(60,60,67,.18); --txt:#000000;
           --muted:rgba(60,60,67,.6); --faint:rgba(60,60,67,.3);
           --accent:#007aff; --good:#34c759; --warn:#ff9500; --crit:#ff3b30;
           --fill:rgba(120,120,128,.12); }
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--txt);
    font-family:-apple-system,"SF Pro Text","Segoe UI Variable Text","Segoe UI",Meiryo,system-ui,sans-serif;
    -webkit-font-smoothing:antialiased;}
  /* macOS のツールバー風: 半透明・スクロールしても常に見える薄いタイトルバー */
  header{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:12px;
    padding:0 20px;height:52px;
    background:color-mix(in srgb, var(--bg) 72%, transparent);
    backdrop-filter:blur(20px) saturate(1.8);-webkit-backdrop-filter:blur(20px) saturate(1.8);
    border-bottom:1px solid var(--line);-webkit-app-region:drag;}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:-.01em}
  .sub{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
  .ver{font-size:10.5px;font-weight:600;color:var(--muted);background:var(--fill);
       border-radius:20px;padding:2px 8px;letter-spacing:.02em}
  .hidebtn{-webkit-app-region:no-drag;margin-left:auto;display:flex;align-items:center;gap:5px;
    border:none;background:var(--fill);color:var(--txt);border-radius:8px;
    padding:6px 12px;font-size:12.5px;font-weight:500;font-family:inherit;cursor:pointer}
  .hidebtn:hover{background:color-mix(in srgb, var(--fill) 160%, transparent)}
  .hidebtn:active{transform:scale(.97)}
  .wrap{max-width:1180px;margin:0 auto;padding:24px 26px 64px}
  h2{font-size:13px;margin:32px 0 12px;font-weight:600;letter-spacing:-.005em;color:var(--txt)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px}
  .card{background:var(--panel);border-radius:14px;padding:14px 16px}
  .card .label{font-size:12px;color:var(--muted);font-weight:500}
  .card .val{font-size:24px;font-weight:600;margin-top:5px;line-height:1.12;letter-spacing:-.02em}
  .card .val small{font-size:12px;font-weight:500;color:var(--muted);letter-spacing:0}
  table{width:100%;border-collapse:collapse;font-size:13px;background:var(--panel);
        border-radius:14px;overflow:hidden;font-variant-numeric:tabular-nums}
  th,td{padding:9px 13px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left;font-variant-numeric:normal}
  th{color:var(--muted);font-weight:600;font-size:11.5px;letter-spacing:.02em}
  tr:last-child td{border-bottom:none}
  .tblwrap{overflow-x:auto}
  /* 棒グラフ (Apple スクリーンタイム風): 本数に応じて行全体へ均等に伸縮し、
     上限幅に達したら中央に寄せる — 少数の棒が左に固まらない */
  .bars{display:flex;align-items:flex-end;justify-content:center;gap:6px;height:132px;
        padding:14px 18px;background:var(--panel);border-radius:14px;overflow-x:auto}
  .bar{flex:1 1 0;min-width:10px;max-width:64px;background:var(--claude);
       border-radius:5px 5px 0 0;position:relative;min-height:2px}
  .bar span{position:absolute;bottom:-16px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--muted);white-space:nowrap}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:820px){.grid2{grid-template-columns:1fr}}
  .muted{color:var(--muted)}
  .pill{display:inline-block;padding:3px 10px;border-radius:20px;background:var(--fill);
        font-size:11px;color:var(--muted);margin:0 6px 6px 0;font-weight:500}
  .note{font-size:11.5px;color:var(--muted);margin-top:8px;line-height:1.5}
  .note code{background:var(--fill);padding:1px 5px;border-radius:5px;font-size:10.5px}
  .err{color:var(--crit)}
  .limits{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
  .gauge{background:var(--panel);border-radius:16px;padding:16px 18px}
  .gauge .top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:11px}
  .gauge .name{font-size:13px;font-weight:600}
  .gauge .rem{font-size:28px;font-weight:600;line-height:1;letter-spacing:-.02em}
  .gauge .rem small{font-size:12px;font-weight:500;color:var(--muted);letter-spacing:0}
  /* 進捗トラックは同色の淡いステップ (Apple の progress と同じ文法) */
  .track{height:8px;border-radius:99px;overflow:hidden}
  .fill{height:100%;border-radius:99px;transition:width .5s ease}
  .g-ok{color:var(--good)}   .bg-ok{background:var(--good)}   .tk-ok{background:rgba(48,209,88,.22)}
  .g-warn{color:var(--warn)} .bg-warn{background:var(--warn)} .tk-warn{background:rgba(255,159,10,.22)}
  .g-crit{color:var(--crit)} .bg-crit{background:var(--crit)} .tk-crit{background:rgba(255,69,58,.22)}
  .gauge .meta{display:flex;justify-content:space-between;margin-top:10px;font-size:11.5px;color:var(--muted)}
  .scoped{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
  .scoped .s{background:var(--panel);border-radius:12px;padding:9px 13px;font-size:12px;min-width:150px;flex:1}
  .scoped .s b{font-size:16px;font-weight:600;letter-spacing:-.01em}
  .stalebadge{font-size:11px;color:var(--warn);margin-left:8px}
  .heat{display:grid;grid-template-columns:repeat(24,1fr);gap:3px;background:var(--panel);border-radius:14px;padding:12px}
  .heat .cell{aspect-ratio:1;border-radius:4px;position:relative}
  .heat .hl{font-size:9px;color:var(--muted);text-align:center;margin-top:3px}
  /* カレンダー (Apple Calendar 風) */
  .calhead{display:flex;align-items:center;gap:14px;margin:2px 0 12px;flex-wrap:wrap}
  .calym{font-size:20px;font-weight:800;letter-spacing:-.015em}
  .seg{display:inline-flex;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:2px;gap:2px}
  .seg button{border:none;background:transparent;color:var(--txt);font-family:inherit;
              font-size:12.5px;font-weight:600;padding:5px 13px;border-radius:8px;cursor:pointer;line-height:1.2}
  .seg button:hover{background:var(--panel)}
  .seg button:active{transform:scale(.97)}
  .caltot{margin-left:auto;display:flex;gap:18px}
  .caltot .t{display:flex;flex-direction:column;align-items:flex-end;line-height:1.2}
  .caltot .t b{font-size:15px;font-weight:700;letter-spacing:-.01em}
  .caltot .t span{font-size:10.5px;color:var(--muted)}
  .cal{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;background:var(--panel);
       border:1px solid var(--line);border-radius:16px;padding:14px}
  .cal .wd{text-align:center;font-size:11px;color:var(--muted);padding-bottom:4px;font-weight:600}
  .cal .wd.sat{color:var(--accent)} .cal .wd.sun{color:#e0574f}
  .cal .day{min-height:80px;border-radius:12px;padding:7px 8px;
            display:flex;flex-direction:column;background:var(--panel2)}
  .cal .day.empty{background:transparent}
  .cal .dn{font-size:11.5px;font-weight:600;color:var(--muted);
           width:20px;height:20px;display:flex;align-items:center;justify-content:center;border-radius:50%}
  .cal .day.today .dn{background:var(--claude);color:#fff;font-weight:700}
  .cal .jpy{font-size:14.5px;font-weight:700;margin-top:auto;line-height:1.15;letter-spacing:-.01em}
  .cal .sub{font-size:9.5px;color:var(--muted);margin-top:1px}
  .cal .none{font-size:10px;color:var(--muted);margin-top:auto;opacity:.4}
</style>
</head>
<body>
<header>
  <h1>Claude 使用状況</h1>
  <span class="ver" id="appver"></span>
  <span class="sub" id="stamp">—</span>
  <button class="hidebtn" id="hideBtn" title="常時表示バーだけを残して閉じる">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M19 13H5v-2h14v2z" fill="currentColor"/></svg>
    非表示にする
  </button>
</header>
<div class="wrap" id="root">読み込み中…</div>
<script>
  (function bindHide(){
    const btn = document.getElementById("hideBtn");
    btn.addEventListener("click", () => {
      fetch("/api/hide", {method:"POST"}).catch(()=>{});
    });
  })();
</script>

<script>
const fmt = n => (n||0).toLocaleString();
const esc = s => String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const money = n => "$" + (n||0).toFixed(2);
const money4 = n => "$" + (n||0).toFixed(4);
function tokM(n){ n=n||0; return n>=1e6 ? (n/1e6).toFixed(2)+"M" : n>=1e3 ? (n/1e3).toFixed(1)+"k" : String(n); }
const WD = ["月","火","水","木","金","土","日"];

function sevClass(rem){
  if(rem==null) return "ok";
  if(rem<=10) return "crit";
  if(rem<=25) return "warn";
  return "ok";
}
function countdown(iso){
  if(!iso) return "";
  const t = new Date(iso).getTime();
  if(isNaN(t)) return "";
  let s = Math.max(0, Math.round((t - Date.now())/1000));
  const d = Math.floor(s/86400); s-=d*86400;
  const h = Math.floor(s/3600); s-=h*3600;
  const m = Math.floor(s/60);
  if(d>0) return `${d}日${h}時間`;
  if(h>0) return `${h}時間${m}分`;
  return `${m}分`;
}
function limGauge(name, w){
  if(!w) return `<div class="gauge"><div class="name">${name}</div><div class="muted" style="margin-top:8px">現在アクティブな枠なし</div></div>`;
  const rem = w.remaining, util = w.utilization;
  const cls = sevClass(rem);
  return `<div class="gauge">
    <div class="top"><span class="name">${name}</span>
      <span class="rem g-${cls}">${rem.toFixed(0)}%<small> 残り</small></span></div>
    <div class="track tk-${cls}"><div class="fill bg-${cls}" style="width:${Math.min(100,util).toFixed(1)}%"></div></div>
    <div class="meta"><span>使用 ${util.toFixed(0)}%</span><span>リセットまで ${countdown(w.resets_at)}</span></div>
  </div>`;
}
function bars(data, valKey, labelFn){
  if(!data || !data.length) return '<div class="muted" style="padding:12px">データなし</div>';
  const recent = data.slice(-60);
  const max = Math.max(...recent.map(d=>d[valKey]||0), 1);
  const el = recent.map(d=>{
    const h = Math.round((d[valKey]||0)/max*96)+2;
    const lbl = labelFn ? labelFn(d) : (String(d.key||"")).slice(5);
    const tip = `${d.key}: ${fmt(d[valKey])}`;
    return `<div class="bar" style="height:${h}px" title="${tip}"><span>${lbl}</span></div>`;
  }).join("");
  return `<div class="bars" style="padding-bottom:26px">${el}</div>`;
}
function heat(hours){
  if(!hours || !hours.length) return "";
  const max = Math.max(...hours.map(d=>d.count||0), 1);
  const cells = hours.map(d=>{
    const a = (d.count||0)/max;
    const bg = a===0 ? "var(--panel2)" : `rgba(214,138,92,${(0.15+0.85*a).toFixed(3)})`;
    return `<div><div class="cell" style="background:${bg}" title="${d.key}時: ${fmt(d.count)}回"></div><div class="hl">${d.key%3===0?d.key:""}</div></div>`;
  }).join("");
  return `<div class="heat">${cells}</div>`;
}
function card(label, val, sub){
  return `<div class="card"><div class="label">${label}</div><div class="val">${val}${sub?` <small>${sub}</small>`:""}</div></div>`;
}

// ===== カレンダー =====
let CAL = null;    // {y, m}  m は 0-11
let LAST = null;   // 直近ペイロード (月移動時の再描画用)
const yen = n => "¥" + Math.round(n||0).toLocaleString();

function calShift(delta){
  if(!CAL) return;
  const d = new Date(CAL.y, CAL.m + delta, 1);
  CAL = {y: d.getFullYear(), m: d.getMonth()};
  if(LAST) render(LAST);
}
function calToday(){
  const n = new Date();
  CAL = {y: n.getFullYear(), m: n.getMonth()};
  if(LAST) render(LAST);
}
window.calShift = calShift;
window.calToday = calToday;

function calendarHtml(p){
  const hist = p.calendar || {};
  if(!CAL){ const n = new Date(); CAL = {y:n.getFullYear(), m:n.getMonth()}; }
  const {y, m} = CAL;
  const first = new Date(y, m, 1);
  const days = new Date(y, m+1, 0).getDate();
  const lead = first.getDay();   // 日曜始まり (getDay: 日=0)
  const todayStr = new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD (ローカル)

  const keyOf = d => `${y}-${String(m+1).padStart(2,"0")}-${String(d).padStart(2,"0")}`;

  let maxJpy = 0, totJpy = 0, totCalls = 0, totTok = 0, activeDays = 0;
  for(let d=1; d<=days; d++){
    const e = hist[keyOf(d)];
    if(!e) continue;
    maxJpy = Math.max(maxJpy, e.cost_jpy||0);
    totJpy += e.cost_jpy||0; totCalls += e.calls||0;
    totTok += (e.in||0)+(e.out||0)+(e.cr||0)+(e.cw5||0)+(e.cw1||0);
    activeDays++;
  }

  let h = `<h2>カレンダー（日別の使用量と推定コスト）</h2>`;
  h += `<div class="calhead">
    <span class="calym">${y}年${m+1}月</span>
    <div class="seg">
      <button onclick="calShift(-1)" title="前月">‹</button>
      <button onclick="calToday()">今日</button>
      <button onclick="calShift(1)" title="翌月">›</button>
    </div>
    <div class="caltot">
      <div class="t"><b>${yen(totJpy)}</b><span>月合計</span></div>
      <div class="t"><b>${fmt(totCalls)}</b><span>呼び出し</span></div>
      <div class="t"><b>${tokM(totTok)}</b><span>tokens</span></div>
      <div class="t"><b>${activeDays}日</b><span>稼働</span></div>
    </div>
  </div>`;

  h += `<div class="cal">`;
  const wd = ["日","月","火","水","木","金","土"];
  wd.forEach((w,i)=>{ h += `<div class="wd ${i===6?"sat":""} ${i===0?"sun":""}">${w}</div>`; });
  for(let i=0;i<lead;i++) h += `<div class="day empty"></div>`;
  for(let d=1; d<=days; d++){
    const k = keyOf(d);
    const e = hist[k];
    const isToday = (k === todayStr);
    let bg = "";
    if(e && maxJpy>0){
      // 単一色相の逐次スケール: 金額が大きい日ほど濃い
      const a = Math.sqrt((e.cost_jpy||0)/maxJpy);  // 低額日も知覚できるよう平方根
      bg = `background:rgba(214,138,92,${(0.08+0.42*a).toFixed(3)})`;
    }
    const tok = e ? (e.in||0)+(e.out||0)+(e.cr||0)+(e.cw5||0)+(e.cw1||0) : 0;
    const body = e
      ? `<div class="jpy">${yen(e.cost_jpy)}</div>
         <div class="sub">${fmt(e.calls)}回 · ${tokM(tok)}</div>`
      : `<div class="none">–</div>`;
    const tip = e ? `${k}\n${yen(e.cost_jpy)} (${money4(e.cost_usd)} @ ${Number(e.rate).toFixed(2)})\n${fmt(e.calls)}回 / ${fmt(tok)} tokens` : k;
    h += `<div class="day${isToday?" today":""}" style="${bg}" title="${tip}">
            <div class="dn">${d}</div>${body}
          </div>`;
  }
  h += `</div>`;
  h += `<div class="note">日本円は取得時の為替で換算し <code>${p.history_path||""}</code> に保存されます。
        過去日はその日に記録したレートを維持、当日は最新レートで再換算します。
        Claude のログが削除された後も履歴は残ります。</div>`;
  return h;
}
function miniTable(title, rows, unit){
  if(!rows || !rows.length) return "";
  let h = `<div><h2 style="margin-top:4px">${title}</h2><div class="tblwrap"><table><tbody>`;
  for(const r of rows){
    h += `<tr><td title="${r.key}">${r.key}</td><td>${fmt(r.count)}${unit||""}</td></tr>`;
  }
  h += `</tbody></table></div></div>`;
  return h;
}

function render(p){
  LAST = p;
  document.getElementById("stamp").textContent = (p.generated_at||"").replace("T"," ");
  document.getElementById("appver").textContent = p.app_version ? "v"+p.app_version : "";
  const c = p.claude;
  const ct = c.totals;
  const jpy = v => "≈¥" + Math.round((v||0)*p.usd_jpy).toLocaleString();
  const rate = p.usd_jpy_meta || {};
  let h = "";

  // ===== 利用上限（サブスク） =====
  const L = p.limits || {};
  h += `<h2 style="margin-top:6px">利用上限（サブスク）`;
  if(L.subscription) h += ` <span class="pill" style="margin-left:8px">${L.subscription}${L.tier?(" / "+L.tier):""}</span>`;
  if(L.stale) h += `<span class="stalebadge">● 直近の取得値（更新待ち）</span>`;
  h += `</h2>`;
  if(!L.available){
    h += `<div class="err" style="padding:6px 0">残量を取得できません: ${L.reason||"不明"}</div>`;
  } else {
    if(L.warn) h += `<div class="stalebadge" style="display:block;margin-bottom:8px">⚠ ${L.warn}</div>`;
    h += `<div class="limits">`
       + limGauge("セッション（5時間枠）", L.five_hour)
       + limGauge("週間（全体）", L.seven_day)
       + limGauge("週間 Opus", L.seven_day_opus)
       + limGauge("週間 Sonnet", L.seven_day_sonnet)
       + `</div>`;
    const scoped = (L.limits||[]).filter(x => x.kind==="weekly_scoped" || (x.group==="weekly" && x.model));
    if(scoped.length){
      h += `<div class="scoped">`;
      for(const s of scoped){
        const rem = s.remaining; const cls = sevClass(rem);
        h += `<div class="s"><div class="muted">週間${s.model?("・"+s.model):""}</div>`
           + `<b class="g-${cls}">${rem!=null?rem+"%":"-"}</b> <span class="muted">残り</span>`
           + `<div class="muted" style="margin-top:2px">リセット ${countdown(s.resets_at)}</div></div>`;
      }
      h += `</div>`;
    }
    h += `<div class="note">Anthropic 利用状況APIの実値。${L.fetched_at?("最終取得 "+L.fetched_at):""}（約60秒間隔でキャッシュ／429回避のため低頻度ポーリング）</div>`;
  }
  h += `<div class="note">USD/JPY ${Number(p.usd_jpy||0).toFixed(2)}（${rate.source||"unknown"}${rate.fallback?" / fallback":""}、1日1回更新）${rate.warn?` <span class="stalebadge">${rate.warn}</span>`:""}</div>`;
  const lan = p.lan || {};
  if(lan.enabled && (lan.urls||[]).length){
    h += `<div class="note">📱 スマホ・他のPCから（同じWi-Fi）: `
      + lan.urls.map(u=>`<code>${u}</code>`).join(" / ")
      + ` — 残量はアカウント全体の実値なので、スマホでの Claude 使用分も反映されます。</div>`;
  }

  // ===== サマリー =====
  h += `<h2>サマリー（ローカルログ実測）</h2>`;
  h += `<div class="cards">`
    + card("推定コスト合計", money(ct.cost), jpy(ct.cost))
    + card("本日のコスト", money(c.today.cost), jpy(c.today.cost))
    + card("API呼び出し", fmt(ct.calls)+"回")
    + card("セッション数", fmt(c.sessions))
    + card("APIエラー", fmt(ct.errors)+"回")
    + card("入力トークン", tokM(ct.in))
    + card("出力トークン", tokM(ct.out))
    + card("キャッシュ読込", tokM(ct.cr), "(0.1x)")
    + card("キャッシュ効率", (c.cache_efficiency||0).toFixed(1)+"%", "読込/総入力")
    + card("Web検索", fmt(ct.web_search)+"回")
    + card("Web取得", fmt(ct.web_fetch)+"回")
    + `</div>`;
  h += `<div class="note">キャッシュ書込 5分TTL ${tokM(ct.cw5)}（1.25x） / 1時間TTL ${tokM(ct.cw1)}（2.0x） ／ 対象ファイル ${fmt(c.files)}件。コストは公開単価による推定値（Web検索は $10/1000req 込み）。</div>`;

  // ===== カレンダー =====
  h += calendarHtml(p);

  // ===== 日別コスト =====
  h += `<h2>日別コスト推移</h2>`;
  h += bars(c.by_date, "cost");

  // ===== 時間帯別ヒートマップ =====
  h += `<h2>時間帯別アクティビティ（0-23時 / API呼び出し）</h2>`;
  h += heat(c.by_hour);

  // ===== 曜日別 =====
  h += `<h2>曜日別アクティビティ</h2>`;
  h += bars(c.by_weekday, "count", d=>WD[d.key]);

  // ===== モデル別 / プロジェクト別 =====
  h += `<div class="grid2" style="margin-top:16px">`;
  h += `<div><h2 style="margin-top:4px">モデル別</h2><div class="tblwrap"><table><thead><tr>`
     + `<th>モデル</th><th>回数</th><th>入力</th><th>出力</th><th>ｷｬｯｼｭ読</th><th>コスト</th></tr></thead><tbody>`;
  for(const r of c.by_model){
    h += `<tr><td>${esc(r.key)}</td><td>${fmt(r.calls)}</td><td>${tokM(r.in)}</td><td>${tokM(r.out)}</td><td>${tokM(r.cr)}</td><td>${money(r.cost)}</td></tr>`;
  }
  h += `</tbody></table></div></div>`;
  h += `<div><h2 style="margin-top:4px">プロジェクト別（上位12）</h2><div class="tblwrap"><table><thead><tr>`
     + `<th>プロジェクト</th><th>ｾｯｼｮﾝ</th><th>回数</th><th>コスト</th></tr></thead><tbody>`;
  for(const r of c.by_project.slice(0,12)){
    const br = (r.branches&&r.branches.length)?` <span class="muted" style="font-size:11px">(${r.branches.slice(0,2).join(", ")})</span>`:"";
    h += `<tr><td title="${r.key}">${r.key}${br}</td><td>${fmt(r.sessions)}</td><td>${fmt(r.calls)}</td><td>${money(r.cost)}</td></tr>`;
  }
  h += `</tbody></table></div></div></div>`;

  // ===== 内訳（バージョン/スキル/停止理由/ティア） =====
  h += `<h2>詳細内訳</h2>`;
  h += `<div class="grid2">`;
  h += miniTable("Claude Code バージョン別", c.by_version, "回");
  h += miniTable("使用スキル別", (c.by_skill&&c.by_skill.length)?c.by_skill:[{key:"（スキル使用なし）",count:0}], "回");
  h += `</div><div class="grid2" style="margin-top:16px">`;
  h += miniTable("停止理由 (stop_reason)", c.by_stop, "回");
  h += miniTable("サービスティア", c.by_tier, "回");
  h += `</div>`;
  const toolRatio = ct.calls ? ((c.by_stop.find(s=>s.key==="tool_use")?.count||0)/ct.calls*100) : 0;
  h += `<div class="note">ツール使用で停止した割合（エージェント的密度）: ${toolRatio.toFixed(1)}%。停止理由 end_turn は応答完結、tool_use はツール呼び出しの継続を示す。</div>`;

  document.getElementById("root").innerHTML = h;
}

async function tick(){
  try{
    const r = await fetch("/api/data", {cache:"no-store"});
    const p = await r.json();
    if(p.error){ document.getElementById("root").innerHTML = '<div class="err">エラー: '+p.error+'</div>'; return; }
    render(p);
  }catch(e){
    document.getElementById("stamp").textContent = "接続エラー: " + e;
  }
}
tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 表示ウィンドウ
#   ブラウザのタブではなく独立したアプリウィンドウで表示する。
#     1) pywebview によるネイティブウィンドウ (Windows: WebView2)
#     2) Edge/Chrome のアプリモード (--app) — タブもアドレスバーも無い専用ウィンドウ
#     3) 通常のブラウザ (最終手段)
# ---------------------------------------------------------------------------
OVERLAY_WIDTH = int(os.environ.get("AI_USAGE_OVERLAY_WIDTH", "640"))
OVERLAY_HEIGHT = int(os.environ.get("AI_USAGE_OVERLAY_HEIGHT", "64"))


def _icon_path():
    """同梱アイコンの実体パス (PyInstaller onefile では展開先を見る)。"""
    import sys
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "app_icon.ico")
    return path if os.path.isfile(path) else None


def _apply_window_icon_async(window):
    """ウィンドウ/タスクバーのアイコンを差し替える (winforms: native は WinForms Form)。

    shown イベント内で Icon を設定すると複数ウィンドウ構成でデッドロックするため、
    native が生えるまで待ってからバックグラウンドスレッドで設定する。
    同じ理由で webview.start(icon=...) も使わない。
    """
    path = _icon_path()
    if not path:
        return

    def run():
        for _ in range(40):
            time.sleep(0.25)
            try:
                form = getattr(window, "native", None)
                if form is None:
                    continue
                from System.Drawing import Icon  # pythonnet 経由
                form.Icon = Icon(path)
                return
            except Exception:
                continue

    threading.Thread(target=run, daemon=True).start()


def _screen_width(webview):
    try:
        screens = list(getattr(webview, "screens", []) or [])
        return screens[0].width if screens else 1920
    except Exception:
        return 1920


def _create_overlay(webview, url, js_api=None):
    """画面上辺に常時最前面の細いバーを出す。他アプリの上にも表示される。"""
    sw = _screen_width(webview)
    width = min(OVERLAY_WIDTH, max(360, sw - 40))
    return webview.create_window(
        "Claude 使用状況バー",
        f"{url}/overlay",
        js_api=js_api,
        frameless=True,
        on_top=True,          # 他のウィンドウを開いても常に見える
        resizable=False,
        easy_drag=True,       # ドラッグで移動可能
        x=max(0, (sw - width) // 2),
        y=0,                  # 画面上辺
        width=width,
        height=OVERLAY_HEIGHT,
        # 既定の最小サイズ (200x100) が効くと高さが詰められないため下げる
        min_size=(320, 24),
    )


def _autofit_overlay(webview, overlay):
    """内容の実測サイズにウィンドウを合わせ、余白を無くして画面上辺の中央に置く。

    body は inline-flex かつ高さ auto なので offsetWidth/offsetHeight が
    内容ぴったりの寸法になる。高DPI では CSS px と物理 px がずれるため、
    現在の窓サイズと innerWidth/innerHeight の比から倍率を較正する。
    """
    def run():
        # 中身が描画される (値が入る) まで待つ
        for _ in range(25):
            time.sleep(0.5)
            try:
                if overlay.evaluate_js(
                        "!!document.getElementById('st') && "
                        "document.getElementById('tt').textContent !== '–'"):
                    break
            except Exception:
                pass
        else:
            return

        sw = _screen_width(webview)
        cur_w, cur_h = OVERLAY_WIDTH, OVERLAY_HEIGHT
        # resize 時の窓枠ぶんのズレを吸収するため、余白が消えるまで数回追い込む
        for _ in range(6):
            try:
                m = overlay.evaluate_js(
                    "[Math.ceil(document.body.offsetWidth),"
                    " Math.ceil(document.body.offsetHeight),"
                    " window.innerWidth, window.innerHeight]")
                if not m or not m[0] or not m[2]:
                    return
                cw, ch, iw, ih = m
                slack_w, slack_h = iw - cw, ih - ch      # CSS px の余り
                if abs(slack_w) <= 2 and abs(slack_h) <= 2:
                    break
                sx = cur_w / float(iw)                    # ≒ デバイスピクセル比
                sy = cur_h / float(ih)
                cur_w = min(max(int(round(cur_w - slack_w * sx)), 320), sw - 40)
                cur_h = min(max(int(round(cur_h - slack_h * sy)), 28), 120)
                overlay.resize(cur_w, cur_h)
                overlay.move(max(0, (sw - cur_w) // 2), 0)
                time.sleep(0.35)
            except Exception:
                return
        try:
            overlay.move(max(0, (sw - cur_w) // 2), 0)  # 最終位置を中央に
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def _native_window(url):
    """pywebview でネイティブウィンドウを開く。閉じられるまでブロック。

    本体の「非表示にする」ボタンと常時表示バーの「開く/終了」ボタンは、
    js_api ではなく /api/hide, /api/show, /api/quit への POST で操作する
    (理由は _window_action_* のコメントを参照)。

    常時表示バーがある場合、本体の閉じるボタン (X) は「隠す」として扱い、
    バーはそのまま残る。完全な終了はバーの ✕ ボタンから行う。
    バーが無い場合のみ、X が従来通りアプリ全体を終了する。
    """
    try:
        import webview
    except Exception:
        return False
    try:
        main = webview.create_window(
            APP_TITLE,
            url,
            width=1320,
            height=940,
            min_size=(900, 600),
            on_top=_flag("AI_USAGE_ON_TOP"),
        )
        _windows["main"] = main

        overlay = None
        if not _flag("AI_USAGE_NO_OVERLAY"):
            try:
                overlay = _create_overlay(webview, url)
                _windows["overlay"] = overlay
                _autofit_overlay(webview, overlay)  # 内容幅に合わせて余白を除く
            except Exception as e:
                _log(f"  常時表示バーを作成できませんでした ({e})。本体のみ表示します。")

        def _overlay_watchdog():
            """バーが何らかの理由で消えたら自動で作り直す (常に表示し続ける)。"""
            time.sleep(15)  # 起動直後の初期化が済んでから監視を始める
            while not _windows.get("quitting"):
                time.sleep(5)
                try:
                    if _windows.get("quitting"):
                        return
                    if main not in webview.windows:
                        return  # アプリ終了中
                    ov = _windows.get("overlay")
                    if ov is None or ov not in webview.windows:
                        _log("  常時表示バーが消えていたため再作成します。")
                        new_ov = _create_overlay(webview, url)
                        _windows["overlay"] = new_ov
                        _autofit_overlay(webview, new_ov)
                except Exception:
                    pass

        if overlay is not None:
            threading.Thread(target=_overlay_watchdog, daemon=True).start()

        if overlay is not None:
            # X = 隠す (バーは残る)。quit_app 経由 (quitting=True) のみ実際に閉じる。
            # 注意: destroy() も closing を発火させるため、フラグ無しで一律
            # キャンセルすると終了できなくなる (検証済み)。
            def _on_main_closing():
                if _windows.get("quitting"):
                    return True   # 許可 → 実際に閉じる
                try:
                    main.hide()
                except Exception:
                    pass
                return False      # キャンセル → 「隠す」として扱う

            def _close_overlay():
                # watchdog が作り直した後のバーも確実に閉じる
                ov = _windows.get("overlay")
                if ov is not None:
                    try:
                        ov.destroy()
                    except Exception:
                        pass

            try:
                main.events.closing += _on_main_closing
                main.events.closed += _close_overlay
            except Exception:
                pass
        else:
            # バーが無いときは X が唯一の終了手段なので誤操作防止の確認を出す
            main.confirm_close = not _flag("AI_USAGE_NO_CONFIRM_CLOSE")

        _apply_window_icon_async(main)

        webview.start()  # 全ウィンドウが閉じられるまでブロック
        return True
    except Exception as e:
        _log(f"  ネイティブウィンドウを開けませんでした ({e})。代替手段を試します。")
        return False


def _browser_app_window(url):
    """Edge/Chrome のアプリモードで専用ウィンドウを開く (タブ無し)。"""
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Google", "Chrome", "Application", "chrome.exe"),
    ]
    profile = os.path.join(HOME, ".ai_usage_dashboard", "window")
    for exe in candidates:
        if not os.path.isfile(exe):
            continue
        try:
            os.makedirs(profile, exist_ok=True)
            subprocess.Popen([
                exe,
                f"--app={url}",
                f"--user-data-dir={profile}",  # 通常のブラウザ窓と混ざらないよう分離
                "--window-size=1320,940",
                "--no-first-run",
                "--no-default-browser-check",
            ])
            return True
        except Exception:
            continue
    return False


def _block_until_interrupt():
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# 多重起動の防止
#   ダブルクリックで2つ同時に立ち上がると、Windows では SO_REUSEADDR により
#   同じポートを両方が掴めてしまい、応答が入り乱れて固まる。
#   名前付きミューテックスで先着1つだけを通し、後発は既存ウィンドウを前面に
#   出して静かに終了する。ポート側も排他バインドにして二重取得を塞ぐ。
# ---------------------------------------------------------------------------
_MUTEX_NAME = "Local\\AIUsageDashboard.SingleInstance"
_mutex_handle = None  # プロセス生存中ずっと保持する


def _acquire_single_instance():
    """先着なら True。既に起動中なら False。"""
    global _mutex_handle
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]

        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        last_error = ctypes.get_last_error()
        if not handle:
            return True  # 判定できないときは起動を妨げない
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle  # 参照を保持 (プロセス終了で自動解放)
        return True
    except Exception:
        return True


def _focus_existing_window():
    """既に起動しているインスタンスの本体を再表示して前面に出す。

    本体は X で「隠す」扱いになるため、まず既存サーバの /api/show を叩いて
    隠れた本体を出す。届かない場合は Win32 でのフォールバック。
    """
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/show", method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=3):
            return
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]

        hwnd = user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """SO_REUSEADDR を切り、同一ポートの二重バインドを Windows でも拒否する。"""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            try:
                # Windows で確実に排他するのは SO_EXCLUSIVEADDRUSE
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
        super().server_bind()


def main():
    # 先に多重起動を弾く (ウィンドウもサーバも作らずに抜ける)
    if not _acquire_single_instance():
        _log("  既に起動しています。既存のウィンドウを前面に出して終了します。")
        _focus_existing_window()
        return

    try:
        server = _ExclusiveHTTPServer((BIND_HOST, PORT), Handler)
    except OSError as e:
        # ミューテックスを取れてもポートが塞がっている場合 (別アプリ等)
        _log(f"  ポート {PORT} を使用できません: {e}")
        _focus_existing_window()
        return

    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"

    _log("=" * 58)
    _log(f"  {APP_TITLE} を起動しました")
    _log(f"  URL: {url}")
    _log("  終了するには ウィンドウを閉じるか このコンソールで Ctrl+C")
    _log("=" * 58)

    try:
        # 互換: AI_USAGE_NO_OPEN / AI_USAGE_NO_WINDOW でサーバのみ起動
        if _flag("AI_USAGE_NO_WINDOW") or _flag("AI_USAGE_NO_OPEN"):
            _log("  ウィンドウ表示は無効です (サーバのみ)。")
            _block_until_interrupt()
        elif _native_window(url):
            pass  # ウィンドウが閉じられた
        elif _browser_app_window(url):
            _log("  アプリモードの専用ウィンドウで表示中です。")
            _block_until_interrupt()
        else:
            _log("  既定のブラウザで開きます。")
            webbrowser.open(url)
            _block_until_interrupt()
    except KeyboardInterrupt:
        pass
    finally:
        _log("\n終了します。")
        server.shutdown()


if __name__ == "__main__":
    main()
