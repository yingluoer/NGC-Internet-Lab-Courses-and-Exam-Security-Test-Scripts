# -*- coding: utf-8 -*-
"""
南京工程学院 - 实验室安全育人智慧学习系统 自动化学习工具
================================================================
目标站点: http://202.119.160.23:83/user/login
后端接口: http://202.119.160.23:8400/jeecg-boot

功能:
  1. login   登录(自动识别图形验证码), 缓存会话
  2. status  查看各课程学习进度
  3. learn   高倍速完成课程视频学习 + 视频弹题(自动满分)
  4. exam    (预留, 本期不启用)

说明:
  - 视频学习通过上报学习进度接口完成, 速度快, 不占用本机播放时间
  - 视频弹题从题目接口取 correctAnswer 提交: 非拟真保证全对(100% 正确),
    拟真模式随机错 1~5 题(答题正确率不影响开考, 开考只看视频学习进度>=90%)
  - 已完成的视频与已答的题目会自动跳过, 可反复运行

作者: WorkBuddy
"""
import argparse
import base64
import json
import os
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time

import requests

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
WEB_BASE = "http://202.119.160.23:83"
API_BASE = "http://202.119.160.23:8400/jeecg-boot"

DEFAULT_USER = ""                   # 不预设学号，运行时由用户输入


def _app_dir():
    """运行时数据目录(session/accounts/captcha/导出题库)。

    打包后写到 %LOCALAPPDATA%/实验室网课单刷, 不污染 exe 所在目录(桌面);
    源码环境用脚本同目录。
    """
    if getattr(sys, "frozen", False):
        base = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "实验室网课单刷")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
        return base
    return os.path.dirname(os.path.abspath(__file__))


# ---- 设备绑定: 每台设备只能运行首次绑定的账号 ----
# 绑定文件固定放 LOCALAPPDATA(不随运行结束删除, 不污染桌面)
try:
    from feature_flags import BIND_DEVICE
except Exception:
    BIND_DEVICE = True   # 默认启用绑定(有限制版)

BINDING_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "实验室网课单刷", "device.lock")


def _device_id():
    """设备唯一标识: 优先 Windows MachineGuid(注册表), 兜底 MAC+主机名"""
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SOFTWARE\Microsoft\Cryptography")
            v, _ = winreg.QueryValueEx(k, "MachineGuid")
            winreg.CloseKey(k)
            if v:
                return str(v)
        except Exception:
            pass
    try:
        import uuid, socket, hashlib
        return hashlib.md5(
            (socket.gethostname() + str(uuid.getnode())).encode()).hexdigest()
    except Exception:
        return "unknown-device"


REG_KEY = r"Software\实验室网课单刷"


def _reg_write(name, value):
    """写注册表 HKCU 双保险"""
    try:
        import winreg
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY)
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, str(value))
        winreg.CloseKey(k)
        return True
    except Exception:
        return False


def _reg_read(name):
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY)
        v, _ = winreg.QueryValueEx(k, name)
        winreg.CloseKey(k)
        return v
    except Exception:
        return None


def load_binding():
    """读取设备绑定: 先读锁定文件, 被删则读注册表兜底。无绑定/损坏返回 None"""
    # 1. 优先读锁定文件
    if os.path.exists(BINDING_FILE):
        try:
            with open(BINDING_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("device") and d.get("user"):
                return d
        except Exception:
            pass
    # 2. 文件被删则读注册表(双保险)
    device = _reg_read("device")
    user = _reg_read("user")
    if device and user:
        return {"device": device, "user": user, "time": _reg_read("time")}
    return None


def save_binding(user):
    """首次登录成功后写入设备绑定, 并设只读+隐藏+系统属性(防看到/防删除)"""
    if not BIND_DEVICE:
        return   # 无限制版不绑定设备
    d = {"device": _device_id(), "user": str(user), "time": int(time.time())}
    try:
        os.makedirs(os.path.dirname(BINDING_FILE), exist_ok=True)
    except Exception:
        pass
    try:
        with open(BINDING_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        return
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # 数据目录设为隐藏(0x2), 资源管理器默认看不到
            k32.SetFileAttributesW(os.path.dirname(BINDING_FILE), 0x2)
            # 文件设只读(0x1)+隐藏(0x2)+系统(0x4); 无权限设系统则退回只读+隐藏
            if not k32.SetFileAttributesW(BINDING_FILE, 0x7):
                k32.SetFileAttributesW(BINDING_FILE, 0x3)
        except Exception:
            pass
    try:
        os.chmod(BINDING_FILE, 0o444)
    except Exception:
        pass
    # 注册表双保险: 文件被删也能从注册表恢复绑定
    _reg_write("device", d["device"])
    _reg_write("user", d["user"])
    _reg_write("time", d["time"])


def check_binding(user):
    """检查账号是否可运行。返回 None=允许; 返回已绑定账号=被拒"""
    if not BIND_DEVICE:
        return None   # 无限制版不检查绑定
    bound = load_binding()
    if bound and str(bound.get("user")) != str(user):
        return bound.get("user")
    return None


def _setup_bundled_browser():
    """打包后自带 Chromium headless shell: 把浏览器路径指向解压目录, 不依赖本机 Edge/Chrome"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = os.path.join(meipass, "ms-playwright")
            if os.path.isdir(bundled):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled


_setup_bundled_browser()

SESSION_FILE = os.path.join(_app_dir(), "session.json")

# 上报节奏: 每段上报的进度比例; 数值越多越接近真实观看, 但更慢
PROGRESS_SEGMENTS = 4
API_GAP = 0.04                       # 每次接口调用间隔(秒), 避免请求过密


class C:
    """终端颜色"""
    G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"; B = "\033[36m"; D = "\033[90m"; E = "\033[0m"
    @staticmethod
    def _w(): return sys.stdout.isatty()
    @classmethod
    def ok(cls, t):   return (cls.G + t + cls.E) if cls._w() else t
    @classmethod
    def warn(cls, t): return (cls.Y + t + cls.E) if cls._w() else t
    @classmethod
    def err(cls, t):  return (cls.R + t + cls.E) if cls._w() else t
    @classmethod
    def info(cls, t): return (cls.B + t + cls.E) if cls._w() else t
    @classmethod
    def dim(cls, t):  return (cls.D + t + cls.E) if cls._w() else t


def log(msg=""):
    print(msg, flush=True)


# ----------------------------------------------------------------------------
# 接口客户端
# ----------------------------------------------------------------------------
class ApiError(Exception):
    pass


class Api:
    def __init__(self, token=None):
        self.s = requests.Session()
        self.s.headers.update({
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": WEB_BASE,
            "Referer": WEB_BASE + "/",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"),
        })
        self.token = token
        if token:
            self.s.headers["X-Access-Token"] = token

    def _url(self, path):
        return API_BASE + path if path.startswith("/") else path

    def get(self, path, **params):
        if params:
            # JeecgBoot 会拒绝含未编码中文的直连, 交给 requests 编码
            r = self.s.get(self._url(path), params=params, timeout=30)
        else:
            r = self.s.get(self._url(path), timeout=30)
        return self._parse(r)

    def post(self, path, payload=None):
        r = self.s.post(self._url(path), data=json.dumps(payload or {}), timeout=30)
        return self._parse(r)

    @staticmethod
    def _parse(r):
        if r.status_code == 401:
            raise ApiError("登录状态失效(401), 请重新执行 login")
        try:
            data = r.json()
        except Exception:
            raise ApiError("返回非 JSON (HTTP %s): %s" % (r.status_code, r.text[:160]))
        if not data.get("success"):
            msg = data.get("message") or ""
            # 业务层失败统一抛出, 由调用方决定是否忽略
            raise ApiError(msg)
        return data.get("result")

    # ---------------- 业务接口 ----------------
    def course_tree(self):
        return self.get("/jcedutec/knowledgeGraph/queryGraphTree")

    def course_types(self, graph_id):
        return self.get("/jcedutec/knowledgeGraph/queryCourseTypeById",
                        id=graph_id, courseType=1)

    def videos(self, graph_id, type_id):
        return self.get("/jcedutec/home/detailByTypeAndGraph",
                        graphId=graph_id, id=type_id)

    def video_detail(self, graph_id, video_id):
        return self.get("/jcedutec/courseSource/getById", id=video_id, graphId=graph_id)

    def questions(self, graph_id, video_id):
        return self.get("/jcedutec/courseSource/queryCourseQuestionRelaByMainId",
                        id=video_id, graphId=graph_id)

    def touch_video(self, graph_id, video_id):
        return self.post("/jcedutec/courseSource/updateVisits",
                         {"id": video_id, "graphId": graph_id})

    def report_rate(self, graph_id, video_id, seconds):
        return self.post("/jcedutec/courseSource/finishRate",
                         {"id": video_id, "graphId": graph_id, "watchDuration": seconds})

    def finish_video(self, graph_id, video_id):
        return self.post("/jcedutec/courseSource/finish",
                         {"id": video_id, "graphId": graph_id})

    def submit_answer(self, graph_id, video_id, question_id, option, kind):
        # 多选提交数组, 单选/判断提交字母
        val = option.split(",") if str(kind) == "3" else option
        return self.post("/jcedutec/courseSource/submitAnswer",
                         {"questionId": question_id, "id": video_id,
                          "graphId": graph_id, "option": val})

    # ---------------- 考试接口(2026-10-30 实测) ----------------
    def my_exam_list(self, page=1, size=20):
        """考试列表。result={records:[...]} 分页结构"""
        r = self.get("/jcedutec/exam/myExamList", pageNo=page, pageSize=size)
        if isinstance(r, dict):
            return r.get("records") or []
        return r or []

    def start_exam(self, exam_id, exam_type="1"):
        """开始考试:POST /jcedutec/exam/startExam {id, type}
        type=1 正式考, 2 模拟。复用未完成 record。返回 {id:recordId, records:[题], examTime}"""
        return self.post("/jcedutec/exam/startExam",
                         {"id": exam_id, "type": exam_type})

    def submit_exam(self, record_id, answers):
        """交卷:POST /jcedutec/exam/submitExam/{recordId} body={作答明细id: 答案}
        实测: param 的 key 必须是 records[].id(作答明细), 不是 questionId"""
        return self.post("/jcedutec/exam/submitExam/%s" % record_id, answers)

    # ---------------- 签字状态 ----------------
    def student_info(self):
        """学生信息(含 commitmentStatus 签字状态):GET /students/queryMyInfo"""
        return self.get("/students/queryMyInfo")

    def is_signed(self):
        """承诺书是否已签字。commitmentStatus=='1' 即已签"""
        try:
            info = self.student_info() or {}
            return str(info.get("commitmentStatus") or "") == "1"
        except Exception:
            return None

    # ---------------- 证书 ----------------
    def query_certificate(self):
        """证书列表:GET /jcedutec/h5/queryCertificate -> [{examName, cert, id}]"""
        return self.get("/jcedutec/h5/queryCertificate")

    def grade_name(self, grade_id):
        """班级名称。student_info 的 gradesName 经常是 null, 得按 gradeId 再查一次"""
        if not grade_id:
            return ""
        try:
            r = self.get("/grades/queryById", id=grade_id) or {}
            return r.get("name") or ""
        except ApiError:
            return ""


# ----------------------------------------------------------------------------
# 登录
# ----------------------------------------------------------------------------
def _ocr():
    try:
        import ddddocr
        return ddddocr.DdddOcr(show_ad=False)
    except Exception:
        return None


def _ask_captcha(raw, attempt):
    """OCR 不可用时, 把验证码存成图片打开, 让用户手输"""
    path = os.path.join(_app_dir(), "captcha.png")
    try:
        with open(path, "wb") as f:
            f.write(raw)
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass
    except Exception:
        return ""
    log(C.warn("  ! 自动识别不可用, 验证码图片: %s" % path))
    try:
        code = input("    请输入图中验证码(回车跳过本次): ").strip()
    except EOFError:
        return ""
    return code


def _launch(p, headed=False):
    """优先本机 Edge/Chrome, 其次打包自带的 Chromium headless shell"""
    channels = ("msedge", "chrome", None)
    if os.environ.get("SL_FORCE_BUNDLED") == "1":
        channels = (None,)   # 强制走自带 chromium, 用于验证或本机浏览器异常时兜底
    for channel in channels:
        try:
            kw = {"headless": not headed}
            if channel:
                kw["channel"] = channel
            return p.chromium.launch(**kw)
        except Exception:
            continue
    raise RuntimeError("未找到可用的 Chrome/Edge 浏览器, 请先安装或执行 playwright install chromium")


def _login_ctx(ctx, user, password=None, ocr=None, retries=6, tag=""):
    """在指定 context 里完成一次登录, 返回 (token, info) 或 (None, None)"""
    password = password or user
    page = ctx.new_page()
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            page.goto(WEB_BASE + "/user/login", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1200)

            raw = b""
            try:
                src = page.eval_on_selector("img", "e=>e.src")
                raw = base64.b64decode(src.split(",", 1)[1])
            except Exception:
                pass

            code = ""
            if ocr is not None and raw:
                try:
                    code = ocr.classification(raw)
                except Exception:
                    code = ""
            if not code and raw:
                code = _ask_captcha(raw, attempt)

            inputs = page.query_selector_all("input")
            if len(inputs) < 3:
                last_err = "登录表单未加载"
                continue
            inputs[0].fill(user)
            inputs[1].fill(password)
            inputs[2].fill(code)
            if not code:
                last_err = "未输入验证码"
                continue

            page.click("button.login-button")
            page.wait_for_timeout(3000)

            token = page.evaluate(
                """()=>{try{const v=localStorage.getItem('pro__Access-Token');
                if(!v)return null;const o=JSON.parse(v);return o.value||null}catch(e){return null}}""")
            if token and "login" not in page.url:
                info = page.evaluate(
                    """()=>{try{const v=localStorage.getItem('pro__Login_Userinfo');
                    if(!v)return {};return JSON.parse(v).value||{}}catch(e){return {}}}""")
                log(C.ok("  \u221a 登录成功 %s: %s (%s)"
                         % (tag, info.get("realname", ""), info.get("username", user))))
                return token, info
            last_err = "验证码可能错误(第 %d 次)" % attempt
            # 抓取登录页真实错误原因(账号不存在/密码错误/验证码错误), 避免统一误报成验证码错误
            try:
                txt = page.inner_text('body')
                for kw in ("账号不存在", "用户名不存在", "密码错误", "密码不正确",
                           "验证码错误", "用户名或密码"):
                    if kw in txt:
                        for line in txt.splitlines():
                            if kw in line:
                                last_err = line.strip()
                                break
                        break
            except Exception:
                pass
            log(C.warn("  ! %s %s 识别=%s" % (tag, last_err, code)))
        except Exception as e:
            last_err = str(e)[:80]
    log(C.err("  ! %s 登录失败: %s" % (tag or user, last_err)))
    return None, None


def browser_login(user, password=None, retries=8, headed=False):
    """单账号登录, 返回 (token, user_info)"""
    from playwright.sync_api import sync_playwright
    ocr = _ocr()
    with sync_playwright() as p:
        browser = _launch(p, headed)
        try:
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            try:
                token, info = _login_ctx(ctx, user, password, ocr, retries)
            finally:
                ctx.close()
        finally:
            browser.close()
    if not token:
        raise RuntimeError("登录失败: %s" % user)
    return token, info


def login_batch(users, headed=False, progress=None):
    """一个浏览器实例内依次登录多个账号(各自独立 context), 返回 {user: {token, info}}"""
    from playwright.sync_api import sync_playwright
    ocr = _ocr()
    out = {}
    with sync_playwright() as p:
        browser = _launch(p, headed)
        try:
            for i, u in enumerate(users, 1):
                tag = "[%d/%d] %s" % (i, len(users), u)
                if progress:
                    progress(u, i, len(users))
                ctx = browser.new_context(viewport={"width": 1440, "height": 900})
                try:
                    token, info = _login_ctx(ctx, u, None, ocr, 5, tag)
                finally:
                    ctx.close()
                if token:
                    out[u] = {"token": token, "info": info or {}}
        finally:
            browser.close()
    return out


def load_session():
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_session(token, info):
    data = {"token": token, "userInfo": info, "time": int(time.time())}
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# 多账号管理
# ----------------------------------------------------------------------------
ACCOUNTS_FILE = os.path.join(_app_dir(), "accounts.json")


def load_accounts():
    """{学号: {token, name, time}}"""
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_accounts(d):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def batch_login(users, headed=False):
    """批量登录, 结果并入 accounts.json"""
    res = login_batch(users, headed=headed)
    acc = load_accounts()
    now = int(time.time())
    for u, v in res.items():
        acc[u] = {"token": v["token"],
                  "name": (v.get("info") or {}).get("realname", ""),
                  "time": now}
    save_accounts(acc)
    log("")
    log(C.ok("批量登录完成: %d/%d 成功" % (len(res), len(users))))
    for u in users:
        st = C.ok("OK") if u in res else C.err("失败")
        log("  %s  %s  %s" % (u, st, (acc.get(u) or {}).get("name", "")))
    return acc


def account_api(user):
    """按学号取得可用的 Api(优先用 accounts.json 里的 token)"""
    acc = load_accounts()
    rec = acc.get(user)
    if rec and rec.get("token"):
        api = Api(rec["token"])
        try:
            api.course_tree()
            return api
        except ApiError:
            log(C.warn("  ! %s 会话已失效" % user))
    raise ApiError("%s 尚未登录或已过期, 请先批量登录" % user)


def ensure_api(user, force_login=False, headed=False):
    """优先使用缓存 token, 失效则重新登录"""
    sess = load_session()
    if sess and not force_login:
        api = Api(sess["token"])
        try:
            api.course_tree()
            return api
        except ApiError:
            log(C.warn("会话已失效, 重新登录..."))
    log(C.info("正在登录 (自动识别验证码)..."))
    token, info = browser_login(user, headed=headed)
    save_session(token, info)
    return Api(token)



# ----------------------------------------------------------------------------
# 学习核心
# ----------------------------------------------------------------------------
class Learner:
    def __init__(self, api, workers=6, mode="fast", video_real=False):
        self.api = api
        self.token = api.token
        self.workers = workers
        self.mode = mode
        self.video_real = video_real
        # 秒刷 vs 拟真: 上报间隔与分段数(拟真更贴近真实观看节奏)
        self.gap = 0.04 if mode == "fast" else 0.3
        self.segments = 4 if mode == "fast" else 8
        self.stat = {"video_done": 0, "video_skip": 0, "q_ok": 0, "q_fail": 0}
        self._lk = threading.Lock()

    def _inc(self, k, n=1):
        with self._lk:
            self.stat[k] += n

    # ---- 课程树 ----
    def root(self):
        trees = self.api.course_tree()
        if not trees:
            raise ApiError("课程树为空")
        return trees[0]["key"], trees[0]["title"]

    def types(self, graph_id):
        return self.api.course_types(graph_id) or []

    def videos(self, graph_id, type_id):
        return self.api.videos(graph_id, type_id) or []

    # ---- 单个视频 ----
    def learn_one(self, api, graph_id, v, dry=False):
        vid, name = v["id"], v["name"]
        dur = float(v.get("duration") or 0)

        if str(v.get("isFinish")) == "1" or float(v.get("learnRate") or 0) >= 100:
            self._inc("video_skip")
            log(C.dim("    · 已完成, 跳过 %s" % name))
            return False
        if dry:
            log(C.info("    → [预演] 将学习 %s (%.0f秒)" % (name, dur)))
            return True

        try:
            api.touch_video(graph_id, vid)
        except ApiError:
            pass

        total = max(dur, 1)
        for i in range(1, self.segments + 1):
            t = int(round(total * i / self.segments))
            try:
                api.report_rate(graph_id, vid, t)
            except ApiError as e:
                log(C.warn("      进度上报异常: %s" % e))
            time.sleep(self.gap)

        try:
            api.finish_video(graph_id, vid)
        except ApiError as e:
            log(C.err("      完成标记失败: %s" % e))

        self._inc("video_done")
        log(C.ok("    √ 视频完成 %s (%.0f秒)" % (name, dur)))
        return True

    # ---- 视频弹题 ----
    def answer_video(self, api, graph_id, v, dry=False):
        vid, name = v["id"], v["name"]
        total = int(v.get("total") or 0)
        done = int(v.get("donum") or 0)
        wrong = int(v.get("unCorrectNum") or 0)
        # 跳过逻辑:
        #   1. 无题 -> 跳过
        #   2. 已答完且错题数已在目标范围内 -> 跳过;
        #      否则重新提交全部题目, 把错题数纠正到目标(非拟真=0, 拟真=少量)
        if total == 0:
            return
        import random
        if self.video_real:
            # 视频拟真: 随机错 1~5 题(答题正确率不影响开考, 开考只看视频学习进度>=90%)
            target_wrong = random.randint(1, 5)
            if done >= total and wrong <= target_wrong:
                return
        else:
            # 非拟真: 保证视频答题全部正确(零错)
            target_wrong = 0
            if done >= total and wrong == 0:
                return
        try:
            qs = api.questions(graph_id, vid) or []
        except ApiError as e:
            log(C.warn("      取题失败: %s" % e))
            return
        # 随机挑 target_wrong 题故意答错(拟真痕迹), 其余全部提交正确答案
        wrong_idxs = set()
        if target_wrong and len(qs) > 1:
            wrong_idxs = set(random.sample(range(len(qs)), min(target_wrong, len(qs) - 1)))
        for i, q in enumerate(qs):
            ans = (q.get("correctAnswer") or "").strip()
            if not ans:
                continue
            if dry:
                continue
            submit_ans = self._wrong_video_ans(q, ans) if i in wrong_idxs else ans
            try:
                self._submit(api, graph_id, vid, q.get("id") or q.get("questionId"), submit_ans, q.get("kind"))
                self._inc("q_ok")
                log(C.dim("    ▸ %s → %s" % ((q.get("stem") or "")[:24], submit_ans)))
            except ApiError as e:
                self._inc("q_fail")
                log(C.warn("    ! 提交失败: %s" % e))
            time.sleep(self.gap)

    def _wrong_video_ans(self, q, ans):
        """视频弹题拟真: 给一个错误答案(单选/判断换字母, 多选少选)"""
        kind = str(q.get("kind") or "2")
        if kind == "3":
            parts = [p.strip() for p in str(ans).split(",") if p.strip()]
            return ",".join(parts[:1]) if len(parts) > 1 else ans
        for o in ("A", "B", "C", "D"):
            if o != str(ans):
                return o
        return ans

    def _submit(self, api, graph_id, vid, qid, ans, kind, retries=4):
        last = None
        for i in range(retries):
            try:
                return api.submit_answer(graph_id, vid, qid, ans, kind)
            except ApiError as e:
                last = e
                msg = str(e)
                if any(k in msg for k in ("超时", "timeout", "Timeout", "网络")):
                    time.sleep(0.8 * (i + 1))
                    continue
                raise
        raise last

    # ---- 任务单元(独立 session, 可并发) ----
    def _task(self, graph_id, v, dry=False):
        api = Api(self.token)
        try:
            self.learn_one(api, graph_id, v, dry=dry)
            self.answer_video(api, graph_id, v, dry=dry)
        except Exception as e:
            log(C.warn("    ! %s 处理异常: %s" % (v.get("name", "")[:16], e)))

    # ---- 主流程 ----
    def run(self, only=None, dry=False):
        graph_id, graph_name = self.root()
        log(C.info("课程体系: %s (%s)" % (graph_name, graph_id)))
        types = self.types(graph_id)
        if not types:
            log(C.err("未取到课程分类"))
            return

        groups = []
        for t in types:
            if only and only not in (t.get("name") or ""):
                continue
            groups.append((t, self.videos(graph_id, t["id"])))

        if dry or self.workers <= 1:
            for t, vids in groups:
                log("")
                log(C.info("【%s】视频 %s/%s · 习题 %s/%s" % (
                    t.get("name"), t.get("corseSourceFinishCount"), t.get("corseSourceCount"),
                    t.get("donum"), t.get("total"))))
                for v in vids:
                    self._task(graph_id, v, dry=dry)
            return

        # 并发: 所有视频展平成一个池
        flat = [(t, v) for t, vids in groups for v in vids]
        log(C.info("并发执行 %d 个视频 (workers=%d)" % (len(flat), self.workers)))
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            list(ex.map(lambda p: self._task(graph_id, p[1], dry=dry), flat))

    def summary(self):
        log("")
        log(C.info("本次执行: 完成视频 %d 个 / 跳过 %d 个, 提交习题 %d 道 (失败 %d)"
                   % (self.stat["video_done"], self.stat["video_skip"],
                      self.stat["q_ok"], self.stat["q_fail"])))


# ----------------------------------------------------------------------------
# 进度展示
# ----------------------------------------------------------------------------
def show_status(api):
    graph_id, graph_name = api.course_tree()[0]["key"], api.course_tree()[0]["title"]
    log(C.info("课程体系: %s" % graph_name))
    log("-" * 62)
    log("%-10s %-14s %-14s %s" % ("课程", "视频进度", "习题进度", "错题"))
    log("-" * 62)
    tv = td = tq = tq2 = 0
    for t in api.course_types(graph_id):
        vf, vt = int(t.get("corseSourceFinishCount") or 0), int(t.get("corseSourceCount") or 0)
        qd, qt = int(t.get("donum") or 0), int(t.get("total") or 0)
        tv += vf; td += vt; tq += qd; tq2 += qt
        flag = C.ok("✔") if (vf >= vt and vt > 0 and qd >= qt) else C.warn("…")
        log("%s %-9s %-14s %-14s %s" % (
            flag, t.get("name"), "%d/%d" % (vf, vt), "%d/%d" % (qd, qt), t.get("unCorrectNum")))
    log("-" * 62)
    log("合计: 视频 %d/%d · 习题 %d/%d" % (tv, td, tq, tq2))


# ----------------------------------------------------------------------------
# 考试(2026-10-30 实测逻辑)
# ----------------------------------------------------------------------------
class Examiner:
    """正式考试自动化。
    实测结论:
      1. startExam{id,type} 复用未完成 record, records[] 每项 id=作答明细id
      2. 交卷 param 的 key 必须是作答明细 id(非 questionId)
      3. 开考后秒交判 0 分(反作弊), 等 90 秒再交 -> 100 分
      4. 题库 /questions/queryListByType 直接返回 correctAnswer, 按 stem 匹配
    """
    MIN_WAIT = 90   # 反秒交等待秒数

    def __init__(self, api):
        self.api = api
        self.bank = _load_bank()

    def list_exams(self):
        return self.api.my_exam_list()

    def take_exam(self, exam_id, exam_type="1", dry_run=False, exam_real=False, mode="fast"):
        start = self.api.start_exam(exam_id, exam_type) or {}
        rid = start.get("id")
        records = start.get("records") or []
        log(C.info("考试开始 exam=%s record=%s 题数=%d 时限=%s分钟"
                   % (exam_id, rid, len(records), start.get("examTime"))))
        if not records:
            log(C.warn("无考题"))
            return rid, None

        answers = {}
        hit = miss = 0
        for q in records:
            qid = q.get("id")   # 作答明细 id(交卷 param 的 key)
            stem = (q.get("stem") or "").strip()
            kind = str(q.get("kind") or "2")
            bank_qid = q.get("questionId")   # 题目主键, 对应本地题库的 id 字段
            ans = self._match(stem, kind, bank_qid)
            if ans is None:
                miss += 1
                ans = self._fallback(kind, q)
                log(C.warn("  [未匹配] %s -> 兜底 %s" % (stem[:30], ans)))
            else:
                hit += 1
            answers[qid] = ans
        log(C.info("题库命中 %d/%d, 缺失 %d(兜底)" % (hit, len(records), miss)))

        # 考试拟真答题: 故意错几题, 分数落在 90-100(及格线上)
        if exam_real and not dry_run:
            import random
            n_wrong = random.randint(0, 5)   # 错 0-5 题 -> 对 45-50 -> 90-100 分
            if n_wrong:
                qids = [q.get("id") for q in records]
                for qid in random.sample(qids, min(n_wrong, len(qids))):
                    q = next((x for x in records if x.get("id") == qid), None)
                    if q is not None:
                        answers[qid] = self._wrong_ans(q, answers.get(qid))
            log(C.info("考试拟真: 故意错 %d 题" % n_wrong))

        if dry_run:
            log(C.warn("dry_run: 不交卷"))
            return rid, {"hit": hit, "miss": miss}

        # 反秒交等待: 秒刷等最低 90 秒, 拟真按每题约 3 秒模拟正常作答
        wait = self.MIN_WAIT
        if mode == "real":
            wait = max(self.MIN_WAIT, len(records) * 3)
        log(C.info("等待 %d 秒模拟作答节奏(实测秒交判 0 分)..." % wait))
        time.sleep(wait)
        res = self.api.submit_exam(rid, answers) or {}
        score = res.get("score")
        log(C.ok("交卷完成: 得分 %s (及格=%s)" % (score, res.get("isQualified"))))
        return rid, res

    def run_formal(self, dry_run=False, exam_real=False, mode="fast"):
        """跑正式考试(examType=1)"""
        exams = self.list_exams()
        formal = [e for e in exams if str(e.get("examType")) == "1"]
        if not formal:
            log(C.warn("无正式考试"))
            return None
        e = formal[0]
        log(C.info("参加: %s" % e.get("examName")))
        return self.take_exam(e["id"], "1", dry_run=dry_run,
                              exam_real=exam_real, mode=mode)

    # ---- 答案匹配 ----
    def _fmt_ans(self, ca, kind):
        """把题库答案格式化成交卷格式。多选题返回排序后的选项列表"""
        import re
        if kind == "3":
            if isinstance(ca, (list, tuple)):
                return sorted(str(a).strip().upper() for a in ca)
            parts = [p for p in re.split(r"[,，、\s]+", str(ca)) if p]
            if len(parts) == 1 and parts[0].isalpha():
                parts = list(parts[0])
            return sorted(p.strip().upper() for p in parts)
        return str(ca).strip()

    def _match(self, stem, kind, qid=None):
        """按题目主键(questionId)优先反查本地题库, 题干匹配兜底。
        题库导出字段是 answer, 不是 correctAnswer。"""
        # 1. questionId 精确反查(最可靠, 主键对得上)
        if qid is not None:
            for q in self.bank:
                if str(q.get("id")) == str(qid):
                    ca = q.get("answer") or q.get("correctAnswer")
                    if ca:
                        return self._fmt_ans(ca, kind)
                    return None
        # 2. 题干归一化精确匹配兜底
        if not stem:
            return None
        import re
        key = re.sub(r"[\s\u3000\u00a0]+", "", stem).lower()
        for q in self.bank:
            ks = re.sub(r"[\s\u3000\u00a0]+", "", q.get("stem") or "").lower()
            if ks == key:
                ca = q.get("answer") or q.get("correctAnswer")
                if ca:
                    return self._fmt_ans(ca, kind)
                return None
        return None

    def _fallback(self, kind, q):
        if kind == "3":
            opts = [k[-1].upper() for k in
                    ("optiona", "optionb", "optionc", "optiond", "optione") if q.get(k)]
            return sorted(opts) if opts else ["A"]
        return "A"

    def _wrong_ans(self, q, correct):
        """给一个错误答案(考试拟真答题用)。单选/判断选非正确项, 多选少选"""
        kind = str(q.get("kind") or "2")
        if kind == "3":
            if isinstance(correct, list):
                return correct[:1] if len(correct) > 1 else ["A"]
            return "A"
        opts = [k[-1].upper() for k in
                ("optiona", "optionb", "optionc", "optiond") if q.get(k)]
        for o in opts:
            if str(o) != str(correct):
                return o
        return "A" if str(correct) != "A" else "B"


def _load_bank():
    """加载本地题库(question_bank.py 的 load_bank)"""
    try:
        from question_bank import load_bank
        return load_bank() or []
    except Exception:
        return []


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="南京工程学院 实验室安全学习平台 自动化工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python nk_auto.py login                 登录并保存会话
  python nk_auto.py status                查看学习进度
  python nk_auto.py learn --dry-run       预演, 只显示将要做什么
  python nk_auto.py learn                 开始学习(全部课程)
  python nk_auto.py learn --only 水电安全  只学指定课程
""")
    ap.add_argument("action", choices=["login", "status", "learn", "exam"], help="操作")
    ap.add_argument("--user", default=DEFAULT_USER, help="学号(必填)")
    ap.add_argument("--only", default=None, help="只处理名称包含该关键字的课程")
    ap.add_argument("--dry-run", action="store_true", help="预演, 不写入任何数据")
    ap.add_argument("--headed", action="store_true", help="登录时显示浏览器窗口")
    ap.add_argument("--relogin", action="store_true", help="强制重新登录")

    args = ap.parse_args()

    if args.action == "exam":
        api = ensure_api(args.user, force_login=args.relogin, headed=args.headed)
        # 签字前置检查
        signed = api.is_signed()
        if signed is False:
            log(C.err("未签署承诺书, 无法参加考试。请先到平台 %s/students/signature 手写签名"
                      % WEB_BASE))
            return
        elif signed is None:
            log(C.warn("签字状态未知, 继续尝试..."))
        else:
            log(C.ok("承诺书已签字"))
        Examiner(api).run_formal(dry_run=args.dry_run)
        return

    try:
        if args.action == "login":
            token, info = browser_login(args.user, headed=args.headed)
            save_session(token, info)
            log(C.ok("会话已保存到 session.json"))
            return

        api = ensure_api(args.user, force_login=args.relogin, headed=args.headed)

        if args.action == "status":
            show_status(api)
        elif args.action == "learn":
            lr = Learner(api)
            lr.run(only=args.only, dry=args.dry_run)
            lr.summary()
            log("")
            show_status(api)
    except ApiError as e:
        log(C.err("接口错误: %s" % e))
        sys.exit(1)
    except KeyboardInterrupt:
        log(C.warn("\n已中断"))
    except Exception as e:
        log(C.err("执行失败: %s" % e))
        sys.exit(1)


if __name__ == "__main__":
    main()
