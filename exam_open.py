# -*- coding: utf-8 -*-
"""
考试流程导航器 — 登录后把浏览器带到「考生承诺书 / 手写签名」页
================================================================
本模块只做导航, 不做提交:

    登录  ->  我的考试  ->  指定考试  ->  点开「开始考试」(触发承诺书弹窗)
          ->  把窗口留在签名框前, 由本人用鼠标手写签名

不代签、不提交承诺书、不调用 startExam、不答题、不交卷。

用法:
    python exam_open.py --user <学号>
    python exam_open.py --user <学号> --exam 机电2026
    python exam_open.py --list-only --user <学号>      # 只列出可参加的考试
    python exam_open.py --user <学号> --no-login       # 复用 session.json 的 token

学号白名单: 自行配置 ID_MIN / ID_MAX(置 0 表示不限制), 范围外的学号直接拒绝。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from nk_auto import (  # noqa: E402
    WEB_BASE, Api, ApiError, C, _launch, _login_ctx, _ocr, load_session, log,
)

# ---------------------------------------------------------------- 白名单
ID_MIN = 0      # 学号白名单下限(置 0 = 不限制)
ID_MAX = 0      # 学号白名单上限(置 0 = 不限制)


def check_user(user):
    """学号必须落在白名单区间内, 否则直接退出, 不进行任何网络操作"""
    u = (user or "").strip()
    if not u.isdigit():
        log(C.err("学号格式不对: %r" % u))
        sys.exit(2)
    n = int(u)
    if ID_MIN and ID_MAX and not (ID_MIN <= n <= ID_MAX):
        log(C.err("学号 %s 不在授权范围内 (%d - %d), 已拒绝登录。" % (u, ID_MIN, ID_MAX)))
        log(C.dim("  本工具限制在 %d - %d 号段内使用。" % (ID_MIN, ID_MAX)))
        sys.exit(3)
    return u


# ---------------------------------------------------------------- 浏览器动作
CANDIDATE_ROLES = ("link", "button", "menuitem", "tab")


def pick(page, text, timeout=3500):
    """按可访问角色 + 文本找第一个可见元素, 找不到返回 None"""
    deadline = timeout
    for _ in range(2):
        for role in CANDIDATE_ROLES:
            try:
                loc = page.get_by_role(role, name=text)
                cnt = loc.count()
            except Exception:
                continue
            for i in range(min(cnt, 4)):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    continue
        try:
            loc = page.get_by_text(text)
            cnt = loc.count()
            for i in range(min(cnt, 4)):
                el = loc.nth(i)
                if el.is_visible():
                    return el
        except Exception:
            pass
        page.wait_for_timeout(deadline // 2)
    return None


def inject_token(page, token):
    """把已有 token 写进 localStorage, 免去再走一次验证码"""
    page.goto(WEB_BASE + "/", wait_until="domcontentloaded", timeout=45000)
    page.evaluate(
        """(t)=>{
            try{
                localStorage.setItem('pro__Access-Token', JSON.stringify({value:t, time:Date.now()}));
            }catch(e){}
        }""",
        token,
    )
    page.reload(wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(1500)


def open_exam_list(page):
    """从首页点进「我的考试」"""
    for text in ("我的考试", "我的考场", "考试中心", "在线考试"):
        el = pick(page, text, timeout=2500)
        if el:
            el.click()
            page.wait_for_timeout(2200)
            log(C.ok("  √ 已进入: %s" % text))
            return True
    log(C.warn("  ! 侧边栏没找到「我的考试」入口, 尝试直接跳转前端路由"))
    for route in ("/exam/myExam", "/exam", "/myExam"):
        try:
            page.goto(WEB_BASE + route, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)
            if "login" not in page.url.lower():
                log(C.ok("  √ 直接进入: %s" % route))
                return True
        except Exception:
            continue
    return False


def exam_cards(page):
    """抓取页面上带「开始考试」的卡片文本, 用于人工确认目标"""
    out = []
    try:
        loc = page.get_by_text("开始考试")
        for i in range(min(loc.count(), 20)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                box = el.bounding_box() or {}
                # 卡片标题一般在这行的上方 40-160px
                ctx_el = el.evaluate("e=>{const c=e.closest('div');return c?c.innerText:''}")
                out.append({"index": i, "x": box.get("x"), "y": box.get("y"),
                            "text": (ctx_el or "")[:120].replace("\n", " | ")})
            except Exception:
                continue
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(
        description="登录并导航到考生承诺书 / 手写签名页(不提交)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python exam_open.py --user <学号>
  python exam_open.py --user <学号> --no-login
  python exam_open.py --list-only --user <学号>
""")
    ap.add_argument("--user", required=True, help="学号, 必须在 %d-%d 之间" % (ID_MIN, ID_MAX))
    ap.add_argument("--password", default=None, help="默认与学号相同")
    ap.add_argument("--exam", default="", help="考试名称关键字, 用于在多场考试里挑一个")
    ap.add_argument("--list-only", action="store_true", help="只列出考试列表, 不进考试")
    ap.add_argument("--no-login", action="store_true", help="用 session.json 里的 token, 不重新登录")
    ap.add_argument("--keep", action="store_true", help="结束时不关浏览器")
    args = ap.parse_args()

    user = check_user(args.user)

    # ---- 取 token ----
    token = None
    if args.no_login:
        sess = load_session() or {}
        token = sess.get("token")
        if not token:
            log(C.err("session.json 里没有 token, 请去掉 --no-login 重新登录"))
            sys.exit(4)
        if (sess.get("userInfo") or {}).get("username") not in (None, user):
            log(C.warn("  ! session.json 属于另一个账号, 已改用登录流程"))
            token = None

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch(p, headed=True)          # 必须带头, 后面要人手签名
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        try:
            if token:
                log(C.info("使用已保存的会话..."))
                inject_token(page, token)
                # 验证 token 是否还有效
                try:
                    Api(token).course_tree()
                    log(C.ok("  √ 会话有效"))
                except ApiError as e:
                    log(C.warn("  ! 会话失效(%s), 改为重新登录" % e))
                    token = None

            if not token:
                log(C.info("正在登录 (自动识别验证码)..."))
                token, info = _login_ctx(ctx, user, args.password, _ocr(), retries=6)
                if not token:
                    log(C.err("登录失败, 流程终止"))
                    sys.exit(5)
                page.goto(WEB_BASE + "/", wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(1500)

            # ---- 进入考试列表 ----
            if not open_exam_list(page):
                log(C.warn("  ! 未能自动定位考试入口, 浏览器已停在首页, 请手动点进「我的考试」"))
                _hand_over(args, browser)
                return

            cards = exam_cards(page)
            if cards:
                log(C.info("  页面上可参加的考试:"))
                for c in cards:
                    log(C.dim("    [%d] %s" % (c["index"], c["text"])))
            else:
                log(C.warn("  ! 页面上没找到「开始考试」按钮, 可能本期无待考科目"))

            if args.list_only:
                log(C.ok("仅列表模式, 结束。"))
                _hand_over(args, browser)
                return

            # ---- 选中目标考试 ----
            target = None
            if args.exam:
                for c in cards:
                    if args.exam in c["text"]:
                        target = c
                        break
                if not target:
                    log(C.warn("  ! 没有名称包含「%s」的考试" % args.exam))
            if target is None and cards:
                target = cards[0]

            if target is not None:
                loc = page.get_by_text("开始考试").nth(target["index"])
                log("")
                log(C.info("待打开: %s" % target["text"]))
                log(C.warn("  下一步会点击「开始考试」, 平台通常在此时弹出《考生承诺书》。"))
                log(C.warn("  如果贵校平台没有弹窗、点下去直接开考, 请先回车变成 Ctrl+C 中断。"))
                try:
                    input(C.info("  确认点击请按回车, 取消请 Ctrl+C: "))
                except EOFError:
                    pass
                loc.click()
                page.wait_for_timeout(2500)
                log(C.ok("  √ 已点击「开始考试」"))
            else:
                log(C.warn("  ! 没有可点击的考试按钮, 浏览器停在当前页"))

            _hand_over(args, browser)

        finally:
            if not args.keep:
                try:
                    ctx.close()
                    browser.close()
                except Exception:
                    pass


def _hand_over(args, browser):
    """把控制权交回给人: 保持窗口, 并在终端提示手写签名"""
    log("")
    log(C.ok("=" * 58))
    log(C.ok(" 浏览器已停在承诺书 / 签名页, 请用鼠标手写签名后自行提交。"))
    log(C.ok(" 本工具不会替你签, 也不会继续往下点。"))
    log(C.ok("=" * 58))
    try:
        input(C.dim("\n处理完毕后按回车结束: "))
    except EOFError:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log(C.warn("\n已中断"))
    except ApiError as e:
        log(C.err("接口错误: %s" % e))
        sys.exit(1)
