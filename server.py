# -*- coding: utf-8 -*-
"""
实验室安全学习工具 · Web 控制台后端
====================================
启动后在本地开一个 HTTP 服务, 浏览器打开即用。

架构:
  浏览器(前端) --HTTP--> Flask(本地) --requests--> 平台接口
                                 --playwright--> 登录(仅登录用一次)

说明:
  登录必须走真实浏览器, 因为平台把密码加密的密钥编译进前端 JS 并做了混淆,
  外部无法复现。登录拿到 token 后所有业务全部走接口, 不再启动浏览器。
"""
import json
import os
import sys
import threading
import time
import webbrowser
from collections import deque

# 控制台编码
if sys.platform == "win32":
    try:
        os.system("chcp 65001 >nul 2>&1")
    except Exception:
        pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, redirect, request, send_from_directory

from nk_auto import (Api, ApiError, Examiner, Learner, WEB_BASE, _app_dir, batch_login,
                     browser_login, load_accounts, save_accounts,
                     save_session, load_session, check_binding, save_binding, BIND_DEVICE)

app = Flask(__name__, static_folder=None)

LOG = deque(maxlen=800)
LOCK = threading.Lock()
STATE = {"user": None, "busy": False, "task": "", "signed": None,
         "exam_score": None, "cert_path": None}

# 存活监测: last=最后心跳时间, bye=页面卸载时间
ALIVE = {"last": time.time(), "bye": 0.0}
IDLE_TIMEOUT = int(os.environ.get("SL_IDLE") or 40)   # 这么久没心跳就认为浏览器已关
BYE_GRACE = 3          # 页面发来卸载通知后, 留几秒给刷新


def resource_dir():
    """静态资源目录。

    打包后优先用 EXE 同目录的 static/（外置，便于修改）；
    找不到再退回 _MEIPASS（万一以后又打进去）。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        if os.path.isdir(os.path.join(base, "static")):
            return base
        return getattr(sys, "_MEIPASS", base)
    return os.path.dirname(os.path.abspath(__file__))


def log(msg, kind="info"):
    with LOCK:
        LOG.append({"t": time.strftime("%H:%M:%S"), "k": kind, "m": str(msg)})


def _wrap_log():
    """把 nk_auto 的 log 输出也接到这里"""
    import nk_auto

    def _l(msg=""):
        s = str(msg)
        if not s.strip():
            return
        k = "ok" if "√" in s else ("err" if "失败" in s or "错误" in s else
                                   ("warn" if "!" in s else "info"))
        log(s.strip(), k)
    nk_auto.log = _l


# ---------------------------------------------------------------------------
# 存活看护: 浏览器关掉后, 本进程自行退出, 不留后台
# ---------------------------------------------------------------------------
@app.route("/api/ping")
def api_ping():
    ALIVE["last"] = time.time()
    ALIVE["bye"] = 0.0          # 又有心跳了, 取消退出
    return "ok"


@app.route("/api/bye", methods=["POST", "GET"])
def api_bye():
    ALIVE["bye"] = time.time()
    # 退出前立即清理账号/会话数据(V/Y 版都不留存), 不依赖进程退出流程
    for _f in ("session.json", "captcha.png", "accounts.json"):
        try:
            _p = os.path.join(_app_dir(), _f)
            if os.path.isfile(_p):
                os.remove(_p)
        except Exception:
            pass
    return "ok"


# ---------------------------------------------------------------------------
# 快捷入口: 直接跳到平台的实名签署页, 由本人手写签名
# ---------------------------------------------------------------------------
@app.route("/api/sign")
def api_sign():
    return redirect(WEB_BASE + "/students/signature")


@app.route("/api/sign_open", methods=["POST"])
def api_sign_open():
    """用 Playwright 打开有头浏览器, 自动登录并跳到签字页, 由用户手写签名"""
    user = STATE["user"]
    if not user:
        return jsonify({"ok": False, "msg": "请先登录"}), 400
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    STATE.update(busy=True, task="打开签字页")
    threading.Thread(target=_do_open_sign, args=(user,), daemon=True).start()
    return jsonify({"ok": True})


def _do_open_sign(user):
    try:
        from playwright.sync_api import sync_playwright
        from nk_auto import _launch, _login_ctx, _ocr, save_session
        ocr = _ocr()
        with sync_playwright() as p:
            browser = _launch(p, headed=True)          # 有头浏览器, 用户能看到
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            token, info = _login_ctx(ctx, user, None, ocr, 8)
            if not token:
                log("签字页: 自动登录失败, 请在浏览器里手动登录", "err")
                STATE["busy"] = False
                return
            # 关键: 保存签字页的新 token, 让原程序与签字页共用同一会话,
            # 避免"两端同时登录"导致原程序 token 失效、无法继续登录
            try:
                save_session(token, info or {})
                log("签字页登录 token 已同步到程序", "dim")
            except Exception:
                pass
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(WEB_BASE + "/students/signature",
                      wait_until="networkidle", timeout=60000)
            log("签字页已打开(已自动登录), 请在浏览器里手写签名", "ok")
            STATE["busy"] = False
            # 保持浏览器打开, 等用户手写签名(最多 5 分钟)
            page.wait_for_timeout(300000)
    except Exception as e:
        log("打开签字页失败: %s" % e, "err")
        STATE["busy"] = False


@app.route("/api/commitment")
def api_commitment():
    return redirect(WEB_BASE + "/students/commitmentView")


def _request_exit():
    """在主线程触发 KeyboardInterrupt, 让 app.run 正常退出并返回,
    从而走 PyInstaller onefile 的 bootloader 清理逻辑, 删除解压的临时目录(_MEI*)。

    不能用 os._exit: 它会直接终止进程、跳过 bootloader 清理, 导致每次运行都在
    %TEMP% 留下一个几百 MB 的 _MEI 解压残留。
    """
    import _thread
    _thread.interrupt_main()


def _watchdog():
    while True:
        time.sleep(2)
        now = time.time()
        if ALIVE["bye"] and now - ALIVE["bye"] > BYE_GRACE:
            print("\n  浏览器已关闭, 进程退出")
            _request_exit()
            return
        if now - ALIVE["last"] > IDLE_TIMEOUT:
            print("\n  长时间无连接, 进程退出")
            _request_exit()
            return


# ---------------------------------------------------------------------------
# 静态页
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(os.path.join(resource_dir(), "static"), "index.html")


@app.route("/static/<path:fn>")
def static_file(fn):
    """静态资源(公告赞赏码图片等)"""
    return send_from_directory(os.path.join(resource_dir(), "static"), fn)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
@app.route("/api/state")
def api_state():
    acc = load_accounts()
    return jsonify({
        "user": STATE["user"],
        "busy": STATE["busy"],
        "task": STATE["task"],
        "signed": STATE.get("signed"),
        "exam_score": STATE.get("exam_score"),
        "cert_path": STATE.get("cert_path"),
        "bind": bool(BIND_DEVICE),
        "accounts": [{"user": u, "name": v.get("name", ""),
                      "hasToken": bool(v.get("token"))} for u, v in acc.items()],
    })


@app.route("/api/logs")
def api_logs():
    since = int(request.args.get("since", 0))
    with LOCK:
        items = list(LOG)
    return jsonify({"total": len(items), "items": items[since:]})


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def _do_login(user):
    try:
        log("接入终端 %s ..." % user, "info")
        token, info = browser_login(user)
        save_session(token, info)
        acc = load_accounts()
        acc[user] = {"token": token, "name": info.get("realname", ""), "time": int(time.time())}
        save_accounts(acc)
        STATE["user"] = user
        # 首次登录成功后, 写入设备绑定(只读文件), 锁定本设备只能跑该账号
        save_binding(user)
        log("握手成功 · %s (%s)" % (info.get("realname", ""), user), "ok")
        # 查签字状态
        try:
            api = Api(token)
            signed = api.is_signed()
            STATE["signed"] = signed
            if signed:
                log("承诺书已签字", "ok")
            elif signed is False:
                log("⚠ 未签字承诺书, 考试前必须先签字!", "warn")
        except Exception:
            STATE["signed"] = None
    except Exception as e:
        log("接入失败: %s" % e, "err")
    finally:
        STATE["busy"] = False


@app.route("/api/login", methods=["POST"])
def api_login():
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    user = (request.json or {}).get("user", "").strip()
    if not user:
        return jsonify({"ok": False, "msg": "请输入学号"}), 400
    # 设备账号绑定: 本设备只允许运行首次绑定的账号
    bound_user = check_binding(user)
    if bound_user:
        return jsonify({"ok": False,
                        "msg": "本设备已绑定账号 %s, 无法运行其他账号" % bound_user}), 403
    STATE.update(busy=True, task="登录")
    threading.Thread(target=_do_login, args=(user,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/switch", methods=["POST"])
def api_switch():
    user = (request.json or {}).get("user", "").strip()
    bound_user = check_binding(user)
    if bound_user:
        return jsonify({"ok": False,
                        "msg": "本设备已绑定账号 %s, 无法运行其他账号" % bound_user}), 403
    acc = load_accounts()
    rec = acc.get(user)
    if not rec or not rec.get("token"):
        return jsonify({"ok": False, "msg": "该账号尚未登录"}), 400
    save_session(rec["token"], {"username": user, "realname": rec.get("name", "")})
    STATE["user"] = user
    STATE["signed"] = None   # 切换账号后重置签字状态, 避免残留上个账号的状态
    log("已切换到 %s" % user, "ok")
    # 历史账号 token 大多已过期: 切过去先验一下, 失效就后台自动重登
    threading.Thread(target=_switch_refresh, args=(user,), daemon=True).start()
    return jsonify({"ok": True})


def _switch_refresh(user):
    """切换账号后静默刷新 token(失败不打扰, 用户操作时还会再试一次)。"""
    try:
        _api_for(user, strict=True)
    except ApiError:
        try:
            _login_user(user)
            log("已自动重新登录: %s" % user, "ok")
            if STATE.get("user") == user:
                try:
                    STATE["signed"] = Api(load_accounts()[user]["token"]).is_signed()
                except Exception:
                    STATE["signed"] = None
        except Exception as e:
            log("账号 %s 自动登录失败: %s (改过密码的账号需手动登录)" % (user, e), "err")


# ---------------------------------------------------------------------------
# 重新登录(V 版: 掉登录/签名后 token 失效时重登)
# ---------------------------------------------------------------------------
@app.route("/api/relogin", methods=["POST"])
def api_relogin():
    user = STATE["user"]
    if not user:
        return jsonify({"ok": False, "msg": "请先登录"}), 400
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    STATE.update(busy=True, task="重新登录")
    threading.Thread(target=_do_relogin, args=(user,), daemon=True).start()
    return jsonify({"ok": True})


def _do_relogin(user):
    try:
        _login_user(user)          # 同时刷新 accounts.json 和 session.json
        log("重新登录成功: %s" % user, "ok")
        try:
            STATE["signed"] = Api(load_accounts()[user]["token"]).is_signed()
        except Exception:
            STATE["signed"] = None
    except Exception as e:
        log("重新登录失败: %s (改过密码的账号需在浏览器里手动登录)" % e, "err")
    finally:
        STATE["busy"] = False
        STATE["task"] = ""


# ---------------------------------------------------------------------------
# 进度
# ---------------------------------------------------------------------------
def _api_for(user, strict=False):
    """取指定账号的 Api。

    strict=True 时绝不回退到全局 session token —— 批量操作必须用账号自己的 token，
    否则会拿当前登录账号的会话去跑别人的学习/考试，等于操作错账号。
    """
    acc = load_accounts()
    rec = acc.get(user) or {}
    if rec.get("token"):
        a = Api(rec["token"])
        try:
            a.course_tree()
            return a
        except ApiError:
            pass
    if strict:
        raise ApiError("账号 %s 登录状态已失效，请先切换到该账号重新登录" % user)
    sess = load_session()
    if sess and sess.get("token"):
        a = Api(sess["token"])
        try:
            a.course_tree()
            return a
        except ApiError:
            pass
    raise ApiError("未登录或会话已过期，请重新接入")


def _name_for(user):
    """账号姓名。优先用 accounts.json 里存的名字(不依赖 token 是否有效)，
    取不到才去问平台 —— 避免批量时 token 回退导致拿成别人的姓名。"""
    acc = load_accounts()
    nm = (acc.get(user) or {}).get("name") or ""
    if nm:
        return nm
    try:
        info = _api_for(user, strict=True).student_info() or {}
        return info.get("name") or ""
    except Exception:
        return ""


def _login_user(user):
    """重新登录指定账号并把新 token 写回 accounts.json。

    平台的 token 有有效期, 历史账号放久了必然 401, 所以跑批前要能自己刷新。
    密码用学校默认值(学号), 改过密码的账号会失败, 需手动在浏览器里登。
    """
    from nk_auto import browser_login
    token, info = browser_login(user, headed=False, retries=6)
    if not token:
        raise ApiError("账号 %s 重新登录失败" % user)
    info = info or {}
    acc = load_accounts()
    rec = acc.get(user) or {}
    rec["token"] = token
    rec["time"] = int(time.time())
    if info.get("realname"):
        rec["name"] = info["realname"]
    acc[user] = rec
    save_accounts(acc)
    # 只有当前选中的账号才覆盖 session.json, 否则会把当前会话顶掉
    if STATE.get("user") == user:
        save_session(token, {"username": user, "realname": rec.get("name", "")})
    return Api(token)


def _api_ensure(user):
    """取账号 Api; token 失效就自动重新登录一次再返回。

    批量跑批和切换历史账号都走这里 —— 历史账号 token 过期是常态,
    不能直接报错让人一个个手动登。
    """
    try:
        return _api_for(user, strict=True), False
    except ApiError:
        log("[%s] 登录已过期, 正在重新登录..." % user, "warn")
        return _login_user(user), True


@app.route("/api/courses")
def api_courses():
    try:
        api = _api_for(STATE["user"] or "")
        gid = api.course_tree()[0]["key"]
        out = []
        tv = td = tq = tq2 = 0
        for t in api.course_types(gid):
            vf, vt = int(t.get("corseSourceFinishCount") or 0), int(t.get("corseSourceCount") or 0)
            qd, qt = int(t.get("donum") or 0), int(t.get("total") or 0)
            tv += vf; td += vt; tq += qd; tq2 += qt
            out.append({"name": t.get("name"), "v": vf, "vt": vt,
                        "q": qd, "qt": qt, "wrong": int(t.get("unCorrectNum") or 0)})
        return jsonify({"ok": True, "items": out,
                        "sum": {"v": tv, "vt": td, "q": tq, "qt": tq2}})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


# ---------------------------------------------------------------------------
# 学习
# ---------------------------------------------------------------------------
def _do_learn(only, mode="fast", video_real=False):
    try:
        api = _api_for(STATE["user"] or "")
        lr = Learner(api, mode=mode, video_real=video_real)
        lr.run(only=only)
        lr.summary()
    except Exception as e:
        log("执行失败: %s" % e, "err")
    finally:
        STATE["busy"] = False


@app.route("/api/learn", methods=["POST"])
def api_learn():
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    d = request.json or {}
    only = d.get("only") or None
    mode = d.get("mode") or "fast"
    video_real = bool(d.get("video_real"))
    STATE.update(busy=True, task="学习")
    threading.Thread(target=_do_learn, args=(only, mode, video_real), daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Y 版多账号批量执行(V 版不可用)
# ---------------------------------------------------------------------------
@app.route("/api/batch", methods=["POST"])
def api_batch():
    if BIND_DEVICE:
        return jsonify({"ok": False, "msg": "V 版不支持多账号批量"}), 403
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    d = request.json or {}
    accounts = d.get("accounts") or []
    action = d.get("action") or "learn"
    if not accounts:
        return jsonify({"ok": False, "msg": "未选择账号"}), 400
    mode = d.get("mode") or "fast"
    video_real = bool(d.get("video_real"))
    exam_real = bool(d.get("exam_real"))
    STATE.update(busy=True, task=("批量登录" if action == "login" else "批量" + action))
    threading.Thread(target=_do_batch, args=(accounts, action, mode, video_real, exam_real), daemon=True).start()
    return jsonify({"ok": True})


def _do_batch(accounts, action, mode, video_real, exam_real):
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def pre_login(user):
            """先并发把所有账号的 token 刷成有效, 避免各线程边跑边登导致看起来是串行。"""
            try:
                _, refreshed = _api_ensure(user)
                return user, refreshed
            except Exception as e:
                log("[%s] 预登录失败: %s" % (user, e), "warn")
                return user, False

        # 登录要开无头浏览器, 并发太高会互相抢资源, 限 3 路
        pre_workers = min(len(accounts), 3)
        log("批量%s: 先刷新 %d 个账号的登录状态..." % (action, len(accounts)), "info")
        with ThreadPoolExecutor(max_workers=pre_workers) as ex:
            for u, ok in ex.map(pre_login, accounts):
                if ok:
                    log("[%s] 登录已刷新" % u, "ok")
        log("登录状态刷新完成, 开始并发执行", "ok")

        def run_one(user):
            try:
                # token 失效自动重新登录, 历史账号放久了必然过期
                api, refreshed = _api_ensure(user)
                if refreshed:
                    log("[%s] 已重新登录" % user, "ok")
                log("[%s] 开始 %s" % (user, action), "info")
                if action == "login":
                    log("[%s] 登录刷新完成" % user, "ok")
                    return user, True
                if action == "learn":
                    lr = Learner(api, mode=mode, video_real=video_real)
                    lr.run()
                    lr.summary()
                elif action == "export":
                    from question_bank import export_bank
                    nm = _name_for(user)
                    name = ("%s_%s" % (user, nm)) if nm else user
                    export_bank(api, name=name)
                elif action == "exam":
                    Examiner(api).run_formal(dry_run=False, exam_real=exam_real, mode=mode)
                    # 批量考试后直接把证书落盘, 文件名 班级-学号-姓名
                    _do_cert_inner(api)
                log("[%s] %s 完成" % (user, action), "ok")
                return user, True
            except Exception as e:
                log("[%s] %s 失败: %s" % (user, action, e), "err")
                return user, False

        # 登录要开无头浏览器, 并发太高会互相抢资源, 登录任务限 2 路
        workers = 2 if action == "login" else min(len(accounts), 4)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fs = {ex.submit(run_one, u): u for u in accounts}
            for f in as_completed(fs):
                try:
                    f.result()
                except Exception:
                    pass
    except Exception as e:
        log("批量执行失败: %s" % e, "err")
    finally:
        STATE["busy"] = False
        STATE["task"] = ""


# ---------------------------------------------------------------------------
# 题库 / 模拟
# ---------------------------------------------------------------------------
def _do_export():
    try:
        from question_bank import export_bank
        user = STATE["user"] or ""
        # Y 版(多账号): 文件名用 学号_姓名, 避免多账号导出互相覆盖
        name = None
        if not BIND_DEVICE:
            nm = _name_for(user)
            name = ("%s_%s" % (user, nm)) if nm else user
        export_bank(_api_for(user), name=name)
    except Exception as e:
        log("导出失败: %s" % e, "err")
    finally:
        STATE["busy"] = False


@app.route("/api/export", methods=["POST"])
def api_export():
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    STATE.update(busy=True, task="导出题库")
    threading.Thread(target=_do_export, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/bank")
def api_bank():
    from question_bank import BANK_JSON, load_bank
    bank = load_bank()
    if not bank:
        return jsonify({"ok": False, "msg": "本地还没有题库"})
    return jsonify({"ok": True, "total": len(bank), "path": BANK_JSON})


@app.route("/api/mock", methods=["POST"])
def api_mock():
    """生成模拟卷。mode=exam 时按真实考试结构出题（5 模块 × 10 题 = 50 题）"""
    import random
    from question_bank import load_bank
    bank = load_bank()
    if not bank:
        return jsonify({"ok": False, "msg": "本地还没有题库"}), 400

    d = request.json or {}
    mode = d.get("mode") or "quick"

    # 真实考试的知识点分布（从 startExam 实际试卷中提取）
    EXAM_MODULES = [
        ("1775441600652918786", "通识安全", 10),
        ("1759832597155233793", "水电安全", 10),
        ("1775441833885581313", "消防安全", 10),
        ("1826149197382914050", "应急处置", 10),
        ("1775442068804354049", "机电安全", 10),
    ]

    if mode == "exam":
        paper, detail = [], []
        for tid, name, n in EXAM_MODULES:
            # 真实试卷只出判断题(1)和单选题(2)，多选不考
            pool = [q for q in bank if q.get("typeId") == tid and q.get("kind") in ("1", "2")]
            if not pool:
                continue
            take = random.sample(pool, min(n, len(pool)))
            paper += take
            detail.append({"name": name, "n": len(take)})
        random.shuffle(paper)
        if not paper:
            return jsonify({"ok": False, "msg": "题库中没有匹配的模块"}), 400
        return jsonify({"ok": True, "paper": paper, "mode": "exam",
                        "detail": detail, "minutes": 30, "pass": 90})

    num = int(d.get("num") or 10)
    types = d.get("types")
    pool = bank
    if types:
        pool = [q for q in bank if any(k in (q.get("type") or "") for k in types)]
    if not pool:
        return jsonify({"ok": False, "msg": "没有匹配的题目"}), 400
    paper = random.sample(pool, min(num, len(pool)))
    return jsonify({"ok": True, "paper": paper, "mode": "quick"})


# ---------------------------------------------------------------------------
# 签字状态 / 考试 / 一键全流程 / 证书(2026-10-30 新增)
# ---------------------------------------------------------------------------
@app.route("/api/sign_status")
def api_sign_status():
    """查询当前用户签字状态"""
    try:
        api = _api_for(STATE["user"] or "")
        signed = api.is_signed()
        STATE["signed"] = signed
        name = None
        try:
            name = api.student_info().get("name")
        except Exception:
            pass
        return jsonify({"ok": True, "signed": signed, "name": name})
    except ApiError as e:
        return jsonify({"ok": False, "msg": str(e)}), 401
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


def _do_exam(mode="fast", exam_real=False):
    try:
        api = _api_for(STATE["user"] or "")
        # 考前再次校验签字
        signed = api.is_signed()
        STATE["signed"] = signed
        if signed is False:
            log("考试中止: 未签署承诺书", "err")
            return
        rid, res = Examiner(api).run_formal(dry_run=False, exam_real=exam_real, mode=mode)
        if res:
            STATE["exam_score"] = res.get("score")
            # 考试完成后自动下载证书到桌面(之前只有一键全流程才下载)
            log("=== 考试完成, 自动下载证书 ===", "info")
            try:
                _do_cert_inner(api)
            except Exception as e:
                log("证书下载失败: %s" % e, "warn")
    except Exception as e:
        log("考试失败: %s" % e, "err")
    finally:
        STATE["busy"] = False
        STATE["task"] = ""


@app.route("/api/exam", methods=["POST"])
def api_exam():
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    # 签字前置校验:未签字直接拒绝(前端弹窗)
    if STATE.get("signed") is False:
        return jsonify({"ok": False, "need_sign": True,
                        "msg": "未签署承诺书, 无法参加考试。\n请先点击「实名签署」完成手写签名。"}), 403
    d = request.json or {}
    mode = d.get("mode") or "fast"
    exam_real = bool(d.get("exam_real"))
    STATE.update(busy=True, task="正式考试")
    threading.Thread(target=_do_exam, args=(mode, exam_real), daemon=True).start()
    return jsonify({"ok": True})


def _do_run(mode="fast", video_real=False, exam_real=False):
    """一键全流程: 题库导出 -> 学习 -> 考试 -> 证书"""
    try:
        api = _api_for(STATE["user"] or "")
        # 签字校验
        signed = api.is_signed()
        STATE["signed"] = signed
        if signed is False:
            log("一键中止: 未签署承诺书", "err")
            return
        log("=== 第 1 步: 导出题库 ===", "info")
        from question_bank import export_bank
        export_bank(api)
        log("=== 第 2 步: 课程学习 ===", "info")
        lr = Learner(api, mode=mode, video_real=video_real)
        lr.run()
        lr.summary()
        log("=== 第 3 步: 正式考试 ===", "info")
        rid, res = Examiner(api).run_formal(dry_run=False, exam_real=exam_real, mode=mode)
        if res:
            STATE["exam_score"] = res.get("score")
        log("=== 第 4 步: 下载证书 ===", "info")
        _do_cert_inner(api)
        log("全流程完成", "ok")
    except Exception as e:
        log("全流程失败: %s" % e, "err")
    finally:
        STATE["busy"] = False
        STATE["task"] = ""


@app.route("/api/run", methods=["POST"])
def api_run():
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    if STATE.get("signed") is False:
        return jsonify({"ok": False, "need_sign": True,
                        "msg": "未签署承诺书, 无法参加考试。\n请先点击「实名签署」完成手写签名。"}), 403
    d = request.json or {}
    mode = d.get("mode") or "fast"
    video_real = bool(d.get("video_real"))
    exam_real = bool(d.get("exam_real"))
    STATE.update(busy=True, task="一键全流程")
    threading.Thread(target=_do_run, args=(mode, video_real, exam_real), daemon=True).start()
    return jsonify({"ok": True})


def _desktop_dir():
    """获取真实桌面路径(支持 OneDrive/自定义位置/中文桌面), 失败兜底 ~/Desktop"""
    if sys.platform == "win32":
        # 1) Windows API: SHGetKnownFolderPath(FOLDERID_Desktop)
        try:
            import ctypes
            from ctypes import wintypes
            class _GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
            fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                        (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
            ptr = ctypes.c_wchar_p()
            hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(ptr))
            if hr == 0 and ptr.value:
                p = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                if os.path.isdir(p):
                    return p
        except Exception:
            pass
        # 2) 注册表兜底
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
            v, _ = winreg.QueryValueEx(k, "Desktop")
            winreg.CloseKey(k)
            v = os.path.expandvars(v)
            if os.path.isdir(v):
                return v
        except Exception:
            pass
    if sys.platform == "darwin":
        # macOS: 标准 ~/Desktop; iCloud 桌面兜底
        d = os.path.join(os.path.expanduser("~"), "Desktop")
        if os.path.isdir(d):
            return d
        icloud = os.path.join(os.path.expanduser("~"),
                              "Library/Mobile Documents/com~apple~CloudDocs/Desktop")
        if os.path.isdir(icloud):
            return icloud
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _student_meta(api):
    """取 班级 / 学号 / 姓名, 用于给导出的证书图片命名。

    gradesName 在平台上经常是 null, 拿 gradeId 再查一次 /grades/queryById。
    """
    info = {}
    try:
        info = api.student_info() or {}
    except Exception:
        pass
    cls = str(info.get("gradesName") or "")
    if not cls and info.get("gradeId"):
        try:
            cls = api.grade_name(info["gradeId"]) or ""
        except Exception:
            cls = ""
    return {"cls": cls, "code": str(info.get("code") or ""),
            "name": str(info.get("name") or "")}


def _safe_name(s):
    """去掉文件名非法字符, 保留中文"""
    return "".join(ch for ch in str(s) if ch not in r'\/:*?"<>|').strip()


def _do_cert_inner(api):
    """下载证书到桌面, 文件名用 班级-学号-姓名"""
    certs = api.query_certificate() or []
    if not certs:
        log("未查询到证书(考试未通过或未生成)", "warn")
        return []
    desktop = _desktop_dir()
    if not os.path.isdir(desktop):
        try:
            os.makedirs(desktop, exist_ok=True)
        except Exception:
            desktop = os.path.expanduser("~")  # 兜底到用户目录
    meta = _student_meta(api)
    parts = [_safe_name(meta["cls"]) or "未知班级",
             _safe_name(meta["code"]) or "未知学号",
             _safe_name(meta["name"]) or "未知姓名"]
    base = "-".join(parts)
    saved = []
    for idx, c in enumerate(certs, 1):
        rel = c.get("cert") or c.get("url") or c.get("fileUrl")
        if not rel:
            continue
        from nk_auto import API_BASE
        url = rel if rel.startswith("http") else API_BASE + "/sys/common/static/" + rel
        ext = ".png" if ".png" in rel.lower() else ".jpg"
        # 多张证书时加序号, 单张不加
        fname = base + (("_%d" % idx) if len(certs) > 1 else "") + ext
        path = os.path.join(desktop, fname)
        try:
            r = api.s.get(url, timeout=60)
            if r.status_code == 200 and len(r.content) > 1000:
                with open(path, "wb") as f:
                    f.write(r.content)
                saved.append(path)
                STATE["cert_path"] = path
                log("证书已下载: %s" % path, "ok")
        except Exception as e:
            log("证书下载失败: %s" % e, "err")
    return saved


def _do_cert():
    try:
        api = _api_for(STATE["user"] or "")
        _do_cert_inner(api)
    except Exception as e:
        log("证书下载失败: %s" % e, "err")
    finally:
        STATE["busy"] = False
        STATE["task"] = ""


@app.route("/api/cert", methods=["POST"])
def api_cert():
    if STATE["busy"]:
        return jsonify({"ok": False, "msg": "有任务正在执行"}), 409
    STATE.update(busy=True, task="下载证书")
    threading.Thread(target=_do_cert, daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
def _find_browser_exe():
    """找 Edge/Chrome 可执行文件路径。优先 Edge, 其次 Chrome。
    360/QQ 等默认浏览器会导致本地服务页打不开或异常, 这里强制用 Edge/Chrome 打开。
    """
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif sys.platform == "win32":
        candidates = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # linux
        candidates = [
            "/usr/bin/microsoft-edge",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _open_browser(url):
    """用 Edge/Chrome 打开本地服务页, 避开 360/QQ 默认浏览器打不开的问题。
    找不到 Edge/Chrome 时, 用打包内置的 Chromium(headed) 打开。
    """
    import subprocess
    exe = _find_browser_exe()
    if exe:
        try:
            subprocess.Popen([exe, url])
            return
        except Exception:
            pass
    # 兜底: 本机没有 Edge/Chrome, 用 playwright 打包内置的 Chromium 打开
    def _bundled():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=False)
                ctx = b.new_context(viewport={"width": 1280, "height": 860})
                pg = ctx.new_page()
                pg.goto(url, wait_until="domcontentloaded")
                while True:   # 保持窗口, 直到进程退出
                    time.sleep(1)
        except Exception:
            pass
    try:
        threading.Thread(target=_bundled, daemon=True).start()
        return
    except Exception:
        pass
    webbrowser.open(url)


def main():
    port = int(os.environ.get("SL_PORT") or 8769)
    for p in range(port, port + 32):
        import socket
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            port = p
            s.close()
            break
        except OSError:
            s.close()
            continue

    _wrap_log()
    # 启动时不预设任何学号，等用户自己输入
    STATE["user"] = None

    url = "http://127.0.0.1:%d/" % port
    print("\n  实验室安全学习终端")
    print("  → %s" % url)
    print("  （关闭浏览器后本程序会自动退出）\n")
    threading.Thread(target=_watchdog, daemon=True).start()
    if not os.environ.get("SL_NOBROWSER"):
        threading.Timer(1.2, lambda: _open_browser(url)).start()
    try:
        app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    # 退出清理: V版(一机一号)和 Y版(测试端)都删除账号/会话, 不留存任何数据
    _clean = ["session.json", "captcha.png", "accounts.json"]
    for _f in _clean:
        try:
            _p = os.path.join(_app_dir(), _f)
            if os.path.isfile(_p):
                os.remove(_p)
        except Exception:
            pass
    # 正常返回后, PyInstaller onefile bootloader 会删除 _MEI 解压目录


if __name__ == "__main__":
    main()
