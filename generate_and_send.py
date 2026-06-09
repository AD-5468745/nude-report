#!/usr/bin/env python3
# 누드TV 일일 · 월누적 리포트 자동 생성 / 텔레그램 채널 발송
import os, io, sys, csv, math, html, datetime, urllib.request
from jinja2 import Template
from playwright.sync_api import sync_playwright
import requests

# ===== 설정 (GitHub Secrets로 주입) =====
SHEET_ID      = os.environ["SHEET_ID"]
GID_RECORDS   = os.environ.get("GID_RECORDS", "0")    # '가입기록' 탭 gid
GID_COMPANIES = os.environ["GID_COMPANIES"]           # '업체목록' 탭 gid
GID_TEXTS     = os.environ.get("GID_TEXTS")           # '문구' 탭 gid (선택). 없으면 기본 문구 사용
BOT_TOKEN     = os.environ["BOT_TOKEN"]
CHANNEL_ID    = os.environ["CHANNEL_ID"]              # @채널아이디 또는 -100xxxxxxxxxx
KST   = datetime.timezone(datetime.timedelta(hours=9))

# ===== 캡션/문구 (구글시트 '문구' 탭에서 수정 가능, 없으면 아래 기본값) =====
# 시트에서 쓸 수 있는 치환 토큰: {년} {월} {일} {합계}
# HTML 링크/굵게 등 그대로 입력 가능:  <a href="https://...">텍스트</a>  ·  <b>강조</b>
DEFAULT_TEXTS = {
    "월누적 캡션":  "📊 누드TV {월}월 누적 신규가입\n🗓 {년}년 {월}월 · 총 {합계}명",
    "업체별 캡션":  "📊 누드TV 파트너 일일 리포트\n🗓 {년}년 {월}월 {일}일 · 총 {합계}명",
    "스포일러 제목": "▼ 전체 업체 펼쳐보기",
}
SCALE = 2                 # 이미지 선명도 배율 (3으로 올리면 더 또렷, 용량↑)
TEMPLATE = "report_template.html"

# ===== 구글 시트 읽기 (API 키 불필요, CSV export) =====
def fetch_csv(gid):
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as r:
        return list(csv.reader(io.StringIO(r.read().decode("utf-8"))))

def parse_date(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try: return datetime.datetime.strptime(s, fmt).date()
        except ValueError: pass
    return None

def load_data():
    # 가입기록: [날짜, 업체명, 신규가입수]  (1행은 헤더)
    records = []
    for row in fetch_csv(GID_RECORDS)[1:]:
        if len(row) < 3 or not row[0].strip(): continue
        d = parse_date(row[0])
        if not d: continue
        try: cnt = int(float(row[2]))
        except (ValueError, IndexError): continue
        records.append((d, row[1].strip(), cnt))
    # 업체목록: [업체명]  (표시 순서, 1행은 헤더)
    companies = [r[0].strip() for r in fetch_csv(GID_COMPANIES)[1:] if r and r[0].strip()]
    return records, companies

# ===== 문구(캡션) 로드: '문구' 탭 [키, 내용] (1행 헤더). 시트 없으면/실패하면 기본값 =====
def load_texts():
    texts = dict(DEFAULT_TEXTS)
    if not GID_TEXTS:
        return texts
    try:
        rows = fetch_csv(GID_TEXTS)
    except Exception as e:
        print("문구 탭 읽기 실패, 기본값 사용:", e)
        return texts
    for row in rows[1:]:
        if len(row) >= 2 and row[0].strip():
            texts[row[0].strip()] = row[1]   # 값은 줄바꿈/HTML 보존 위해 strip 안 함
    return texts

def fill(text, **tokens):
    for k, v in tokens.items():
        text = text.replace("{" + k + "}", str(v))
    return text

# ===== 다단 레이아웃 (줄 수에 따라 자동 열 분할) =====
def layout(n):
    c = 1 if n <= 15 else 2 if n <= 32 else 3 if n <= 66 else 4
    return c, {1: 900, 2: 1000, 3: 1260, 4: 1480}[c]

def split_cols(rows, c):
    per = math.ceil(len(rows) / c) if rows else 1
    return [rows[i*per:(i+1)*per] for i in range(c)]

def Row(cells, empty=False):
    return {"cells": cells, "empty": empty}

def build_html(tpl, title, total, headers, rows):
    c, cw = layout(len(rows))
    return tpl.render(title=title, total=total, headers=headers,
                      columns=split_cols(rows, c), card_w=cw, canvas_w=cw + 144)

# ===== HTML → PNG (헤드리스 크롬, .canvas 영역만 캡처) =====
def render_png(page, html_str, path):
    page.set_content(html_str, wait_until="networkidle")
    page.evaluate("document.fonts.ready")   # 폰트 로드 완료 대기 (한글 깨짐 방지)
    page.wait_for_timeout(300)
    page.locator(".canvas").screenshot(path=path)

# ===== 텔레그램 =====
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
def tg(method, data, files=None):
    j = requests.post(f"{API}/{method}", data=data, files=files).json()
    if not j.get("ok"):
        raise RuntimeError(f"{method} 실패: {j}")

def send_photo(path, caption):
    with open(path, "rb") as f:
        tg("sendPhoto", {"chat_id": CHANNEL_ID, "caption": caption,
                         "parse_mode": "HTML"}, files={"photo": f})

def send_message(text):
    tg("sendMessage", {"chat_id": CHANNEL_ID, "text": text,
                       "parse_mode": "HTML", "disable_web_page_preview": True})

# ===== 메인 =====
# mode: "month"=월누적만(낮12시) / "day"=업체별+스포일러만(저녁8시) / "all"=전부(수동테스트)
def main(mode="all"):
    do_month = mode in ("all", "month")
    do_day   = mode in ("all", "day")

    records, companies = load_data()
    target = (datetime.datetime.now(KST) - datetime.timedelta(days=1)).date()  # 전날
    y, m, d = target.year, target.month, target.day

    tpl = Template(open(TEMPLATE, encoding="utf-8").read())
    month_html = day_html = None

    if do_month:
        # (1) 월 누적: target이 속한 달의 일자별 합계
        by_day = {}
        for dt, _, cnt in records:
            if dt.year == y and dt.month == m:
                by_day[dt] = by_day.get(dt, 0) + cnt
        month_rows  = [Row([f"{m}월 {dt.day}일", v]) for dt, v in sorted(by_day.items())]
        month_total = sum(by_day.values())
        # 캡션 {일자별} 토큰용: "M월 D일 — N명" 줄들
        month_daily = "\n".join(f"{m}월 {dt.day}일 — {v}명" for dt, v in sorted(by_day.items()))
        month_html = build_html(tpl, f"{m}월 제휴업체 총 신규가입", month_total,
                                ["신규 가입자", "신규 가입자 수"], month_rows)

    if do_day:
        # (2) 업체별(전날): 가입>0 업체만, 업체목록 순서 + 목록 밖 업체는 뒤에
        day_counts = {}
        for dt, name, cnt in records:
            if dt == target:
                day_counts[name] = day_counts.get(name, 0) + cnt
        full_order = companies + [n for n in day_counts if n not in companies]
        active = [(n, day_counts.get(n, 0)) for n in full_order if day_counts.get(n, 0) > 0]
        day_rows  = [Row([i + 1, n, v]) for i, (n, v) in enumerate(active)]
        day_total = sum(v for _, v in active)
        # 캡션 {업체별} 토큰용: "N. 업체명 — N명" 줄들 (가입>0만)
        day_list = "\n".join(f"{i + 1}. {html.escape(n)} — {v}명" for i, (n, v) in enumerate(active))
        # (3) 전체 업체 캡션(0 포함, 시트 순서대로 세로 나열)
        spoiler = "\n".join(f"{html.escape(n)} {day_counts.get(n, 0)}" for n in full_order)
        day_html   = build_html(tpl, f"{m}월 {d}일 총 가입자", day_total,
                                ["NO", "업체명", "신규 가입 회원수"], day_rows)

    with sync_playwright() as p:
        br  = p.chromium.launch()
        ctx = br.new_context(viewport={"width": 1700, "height": 1200}, device_scale_factor=SCALE)
        page = ctx.new_page()
        if do_month: render_png(page, month_html, "month.png")
        if do_day:   render_png(page, day_html,   "day.png")
        br.close()

    # 발송: 월 누적(낮12시) / 업체별 + 전체 스포일러(저녁8시)
    # 캡션은 시트 '문구' 탭에서 가져옴(없으면 기본값). HTML 링크 그대로 살림.
    texts = load_texts()
    if do_month:
        cap = fill(texts["월누적 캡션"], 년=y, 월=m, 일=d, 합계=month_total, 일자별=month_daily)
        send_photo("month.png", cap)
        print("월누적 발송 완료:", target, "| 총", month_total)
    if do_day:
        cap = fill(texts["업체별 캡션"], 년=y, 월=m, 일=d, 합계=day_total, 업체별=day_list)
        send_photo("day.png", cap)
        title = fill(texts["스포일러 제목"], 년=y, 월=m, 일=d, 합계=day_total)
        send_message(f"{title}\n<tg-spoiler>{spoiler}</tg-spoiler>")
        print("업체별 발송 완료:", target, "| 총", day_total)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode not in ("all", "month", "day"):
        sys.exit(f"알 수 없는 모드: {mode} (month/day/all 중 하나)")
    main(mode)
