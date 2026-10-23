#!/usr/bin/env python3
"""네이버 플레이스 예매 목록에 키워드 상품이 새로 생기면 지정 날짜로 예매를 진행하고 ntfy로 폰에 알린다.

사용 예:
  python watcher.py check                      # 한 번 검사 (GitHub Actions가 10분마다 실행)
  python watcher.py login                      # 로컬 테스트용: 크롬 창에서 직접 네이버 로그인
  python watcher.py check --keyword "주중 입장권" --date 2026-11-06 --auth .naver_auth.json --submit --headed --no-state
"""
import argparse
import asyncio
import json
import os
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import async_playwright

PLACE_ID = "12019039"  # 백제문화단지
LIST_URL = f"https://pcmap.place.naver.com/place/{PLACE_ID}/ticket?from=map&locale=ko"
MAP_URL = f"https://map.naver.com/p/entry/place/{PLACE_ID}?placePath=%2Fticket%3Ffrom%3Dmap%26locale%3Dko"
LOGIN_URL = "https://nid.naver.com/nidlogin.login"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
KST = timezone(timedelta(hours=9))
NEXT_BTN = 'button[data-click-code="nextbuttonview.request"]'
SUBMIT_BTN = 'button[data-click-code="submitbutton.submit"]'  # 주문서의 "동의하고 결제하기"
SUBMIT_TEXT = re.compile(r"결제하기|예약하기|예약 신청|신청하기")
SHOTS = Path("shots")

try:  # macOS python.org 빌드는 기본 인증서가 없어 certifi를 쓴다
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = None


def log(*args):
    print(datetime.now(KST).strftime("%H:%M:%S"), *args, flush=True)


def notify(title, message, click=None, priority=4, tags=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log("NTFY_TOPIC 미설정 - 알림 생략:", title)
        return
    body = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        body["tags"] = tags
    if click:
        body["click"] = click
        body["actions"] = [{"action": "view", "label": "예매 페이지 열기", "url": click}]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    req = urllib.request.Request(server, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
        log("알림 전송:", r.status, title)


def with_date(href, day):
    """예매 URL에 startDateTime을 넣어 해당 날짜가 열린 상태로 들어가게 한다."""
    parts = urlsplit(href)
    q = dict(parse_qsl(parts.query))
    q["startDateTime"] = f"{day}T00:00:00+09:00"
    return urlunsplit(parts._replace(query=urlencode(q)))


async def list_items(page):
    """예매 탭의 상품 목록을 [{id, name, href}]로 돌려준다."""
    js = r"""(root) => {
      const out = {};
      for (const a of root.querySelectorAll('a[href*="booking.naver.com/booking/"][href*="/items/"]')) {
        const m = a.href.match(/\/items\/(\d+)/); if (!m) continue;
        const name = ((a.innerText || '').split('\n')[0] || a.querySelector('img')?.alt || '').trim();
        if (!out[m[1]] || (!out[m[1]].name && name)) out[m[1]] = {id: m[1], name, href: a.href};
      }
      return Object.values(out);
    }"""
    try:
        resp = await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30000)
        if resp and resp.status == 200:
            await page.wait_for_selector('a[href*="/items/"]', timeout=15000)
            return await page.evaluate(f"({js})(document)")
        log("목록 페이지 응답", resp.status if resp else None, "- 지도 페이지로 재시도")
    except Exception as e:
        log("목록 페이지 실패 - 지도 페이지로 재시도:", e)
    await page.goto(MAP_URL, wait_until="domcontentloaded", timeout=30000)
    frame = None
    for _ in range(30):
        frame = page.frame(name="entryIframe")
        if frame:
            break
        await page.wait_for_timeout(500)
    if not frame:
        raise RuntimeError("지도 페이지에서 예매 목록 iframe을 찾지 못함")
    await frame.wait_for_selector('a[href*="/items/"]', timeout=20000)
    return await frame.evaluate(f"({js})(document)")


async def pick_date(page, day):
    """달력에서 day(YYYY-MM-DD)를 고른다. 결과 상태 문자열을 돌려준다."""
    y, m, d = (int(x) for x in day.split("-"))
    title = page.locator(".calendar_title").first
    await title.wait_for(timeout=20000)
    want = f"{y}.{m:02d}"
    for _ in range(12):
        if want in (await title.inner_text()):
            break
        nxt = page.locator('button[data-click-code="calendar.nextmonth"]').first
        if await nxt.is_disabled():
            return "no_month"
        await nxt.click()
        await page.wait_for_timeout(400)
    else:
        return "no_month"

    cells = page.locator("button.calendar_date").filter(
        has=page.locator("span.num", has_text=re.compile(rf"^{d}$")))
    n = await cells.count()
    if n == 0:
        return "no_day"
    # 앞뒤 달 날짜가 같이 보이므로, 월초는 첫 번째·월말은 마지막 칸이 해당 월이다
    cell = cells.first if d < 15 else cells.nth(n - 1)
    text = await cell.inner_text()
    cls = await cell.get_attribute("class") or ""
    if "마감" in text:
        return "sold_out"
    if await cell.is_disabled() or "unselectable" in cls:
        return "unavailable"
    await cell.click()
    await page.wait_for_timeout(800)
    return "selected"


async def pick_time_and_qty(page):
    """회차가 있으면 첫 가능 회차를 고르고, 첫 번째 권종 수량을 1로 올린다."""
    slots = page.locator('button[data-click-code*="time"]:not([disabled]), '
                         'button[data-click-code*="schedule"]:not([disabled])')
    if await slots.count():
        await slots.first.click()
        await page.wait_for_timeout(600)
    plus = page.get_by_role("button", name=re.compile("수량 증가"))
    try:
        await plus.first.wait_for(timeout=5000)
    except Exception:
        return False
    for i in range(await plus.count()):
        b = plus.nth(i)
        if await b.is_enabled():
            await b.click()
            await page.wait_for_timeout(400)
            return True
    return False


async def submit_order(page):
    """주문서 페이지의 최종 예매 버튼을 누른다. 네이버페이 결제창이 뜨면 거기서 멈춘다."""
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(2500)
    btn = page.locator(SUBMIT_BTN)
    if not await btn.count():
        btn = page.get_by_role("button", name=SUBMIT_TEXT).last
    if not await btn.count():
        return "submit_button_not_found"
    label = (await btn.inner_text()).strip().replace("\n", " ")
    log("최종 버튼 클릭:", label)
    popup = None
    try:
        async with page.context.expect_page(timeout=8000) as pi:
            await btn.click()
        popup = await pi.value
    except Exception:
        pass
    target = popup or page
    try:
        await target.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await target.wait_for_timeout(1500)
    await target.screenshot(path=str(SHOTS / "after_submit.png"))
    return f"submitted:{label} -> {target.url[:120]}"


async def run_booking(ctx, item, day, submit):
    """상품 페이지에 들어가 날짜 선택 → 수량 1 → 다음(→ 로그인 상태면 최종 버튼)까지 진행."""
    page = await ctx.new_page()
    url = with_date(item["href"], day)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    status = await pick_date(page, day)
    log("날짜 선택:", status)
    if status != "selected":
        await page.screenshot(path=str(SHOTS / f"date_{item['id']}.png"), full_page=True)
        return status, url
    if not await pick_time_and_qty(page):
        await page.screenshot(path=str(SHOTS / f"qty_{item['id']}.png"), full_page=True)
        return "no_quantity", url
    nxt = page.locator(NEXT_BTN)
    if "disabled" in (await nxt.get_attribute("class") or ""):
        await page.screenshot(path=str(SHOTS / f"next_{item['id']}.png"), full_page=True)
        return "next_disabled", url
    await nxt.click()
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(2000)
    await page.screenshot(path=str(SHOTS / f"after_next_{item['id']}.png"), full_page=True)
    log("다음 클릭 후:", page.url[:120])
    if "nid.naver.com" in page.url:
        return "next_clicked_login_required", url
    if not submit:
        return "next_clicked_order_form", url
    return await submit_order(page), url


STATUS_KO = {
    "selected": "날짜 선택됨",
    "sold_out": "마감",
    "unavailable": "선택 불가(휴무/판매 전)",
    "no_month": "해당 월 달력 없음(판매 전)",
    "no_day": "달력에 날짜 없음",
    "no_quantity": "수량 선택 불가",
    "next_disabled": "다음 버튼 비활성",
    "next_clicked_login_required": "예매 가능! 날짜·수량 선택 후 '다음'까지 눌렀습니다",
    "next_clicked_order_form": "예매 가능! 주문서 화면까지 진행했습니다",
}
AVAILABLE = ("next_clicked", "submitted")


def describe(status):
    if status.startswith("submitted:"):
        return "예매 가능! 최종 버튼까지 눌렀습니다 (" + status.split(":", 1)[1] + ")"
    return STATUS_KO.get(status, status)


def load_state(path):
    if path and Path(path).exists():
        return json.loads(Path(path).read_text())
    return {"seen": {}, "fail_streak": 0}


def save_state(path, state):
    if path:
        Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=1))


async def check(args):
    SHOTS.mkdir(exist_ok=True)
    today = datetime.now(KST).date().isoformat()
    if today > args.date:
        log(f"목표 날짜({args.date})가 지나 검사하지 않음")
        return 0
    state = load_state(args.state)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=not args.headed)
        ctx = await browser.new_context(locale="ko-KR", user_agent=UA, viewport={"width": 480, "height": 900},
                                        storage_state=args.auth or None)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                items = await list_items(page)
            except Exception as e:
                state["fail_streak"] = state.get("fail_streak", 0) + 1
                save_state(args.state, state)
                log("목록 조회 실패:", e)
                await page.screenshot(path=str(SHOTS / "list_error.png"))
                if state["fail_streak"] == 3 or state["fail_streak"] % 36 == 0:
                    notify("감시 스크립트 오류", f"예매 목록을 {state['fail_streak']}회 연속 못 읽었습니다: {e}",
                           priority=3, tags=["warning"])
                return 1
            state["fail_streak"] = 0
            log(f"목록 {len(items)}개:", " / ".join(i["name"] for i in items))
            hits = [i for i in items if args.keyword in i["name"]]
            if not hits:
                log(f"'{args.keyword}' 상품 없음")
            for item in hits:
                log("발견:", item["id"], item["name"])
                try:
                    status, url = await run_booking(ctx, item, args.date, args.submit)
                except Exception as e:
                    status, url = f"자동 진행 실패: {e}".split("\n")[0][:200], with_date(item["href"], args.date)
                log("결과:", status)
                prev = state["seen"].get(item["id"])
                ok = status.startswith(AVAILABLE)
                was_ok = bool(prev) and prev["status"].startswith(AVAILABLE)
                if prev is None or (ok and not was_ok):
                    title = (f"[{args.keyword}] {args.date[5:].replace('-', '/')} 예매 가능!" if ok
                             else f"[{args.keyword}] 예매 상품 등장")
                    notify(title, f"{item['name']}\n{args.date}: {describe(status)}\n눌러서 바로 결제하세요.",
                           click=url, priority=5 if ok else 4, tags=["rotating_light" if ok else "bell"])
                state["seen"][item["id"]] = {"name": item["name"], "status": status,
                                             "checked": datetime.now(KST).isoformat(timespec="seconds")}
            save_state(args.state, state)
            if args.headed and args.submit and hits:
                await page.wait_for_timeout(5000)
            return 0
        finally:
            await browser.close()


async def login(args):
    """로컬 테스트용: 열린 크롬 창에서 직접 네이버에 로그인하면 쿠키를 파일로 저장하고 창을 닫는다."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False)
        ctx = await browser.new_context(locale="ko-KR", user_agent=UA, no_viewport=True)
        page = await ctx.new_page()
        await page.goto(LOGIN_URL)
        print("열린 크롬 창에서 네이버에 로그인해 주세요. 로그인되면 창이 자동으로 닫힙니다.", flush=True)
        for _ in range(600):
            if any(c["name"] == "NID_AUT" for c in await ctx.cookies("https://nid.naver.com")):
                await page.wait_for_timeout(2000)
                await ctx.storage_state(path=args.auth)
                print("로그인 쿠키 저장:", args.auth, flush=True)
                break
            await asyncio.sleep(1)
        else:
            print("10분 안에 로그인이 확인되지 않았습니다.", flush=True)
            return 1
        await browser.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--keyword", default=os.environ.get("KEYWORD") or "낙화놀이")
    c.add_argument("--date", default=os.environ.get("TARGET_DATE") or "2026-11-07")
    c.add_argument("--state", default="state.json")
    c.add_argument("--no-state", dest="state", action="store_const", const="")
    c.add_argument("--auth", help="login 명령으로 저장한 네이버 로그인 쿠키 파일 (로컬 테스트용)")
    c.add_argument("--submit", action="store_true", help="로그인 상태면 주문서의 최종 버튼까지 누른다")
    c.add_argument("--headed", action="store_true")
    lg = sub.add_parser("login")
    lg.add_argument("--auth", default=".naver_auth.json")
    args = ap.parse_args()
    sys.exit(asyncio.run(check(args) if args.cmd == "check" else login(args)) or 0)


if __name__ == "__main__":
    main()
