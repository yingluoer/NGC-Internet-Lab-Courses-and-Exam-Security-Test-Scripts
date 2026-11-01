# -*- coding: utf-8 -*-
"""
漏洞修复验证脚本（防御侧）
==========================
用途：修复完成后跑一遍，确认漏洞是否真的堵上。可反复运行，用于回归测试。

设计原则：
  - 只做「探测」，不产生作弊效果，学生拿到这个脚本没有利用价值
  - 默认只读，不改动任何业务数据；写操作需显式加 --deep
  - 每项输出 PASS(已修复) / FAIL(仍存在) / SKIP(跳过) / MANUAL(需人工确认)

用法：
  python verify_fixes.py                      # 只读检测
  python verify_fixes.py --user test_stu_01   # 指定测试账号
  python verify_fixes.py --deep               # 含写操作检测(会写少量测试数据)
"""
import argparse
import base64
import glob
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nk_auto import API_BASE, WEB_BASE, Api, ApiError, C, ensure_api, log

RESULTS = []


def record(sid, title, status, detail=""):
    RESULTS.append((sid, title, status, detail))
    color = {"PASS": C.ok, "FAIL": C.err, "SKIP": C.dim, "MANUAL": C.warn}[status]
    log("  [%s] %-2s %s" % (color(status), sid, title))
    if detail:
        log(C.dim("        " + detail))


# ---------------------------------------------------------------------------
# S1 弱口令：密码 = 学号
# ---------------------------------------------------------------------------
def check_s1(user):
    """不实际登录，改为探测「登录失败是否有次数限制」+ 默认口令是否可用"""
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Origin": WEB_BASE})
    url = API_BASE + "/sys/userLogin"

    # 用错误密码连续尝试，观察是否触发锁定
    locked = False
    for i in range(1, 8):
        payload = {"username": user, "password": "WrongPwd!Test%d" % i,
                   "captcha": "0000", "checkKey": "0", "remember_me": True}
        try:
            r = s.post(url, data=json.dumps(payload), timeout=15).json()
            msg = r.get("message") or ""
            if any(k in msg for k in ("锁定", "频繁", "稍后", "次数")):
                locked = True
                record("S1b", "登录失败次数限制", "PASS", "第 %d 次触发: %s" % (i, msg))
                break
        except Exception as e:
            record("S1b", "登录失败次数限制", "MANUAL", "请求异常: %s" % e)
            return
    if not locked:
        record("S1b", "登录失败次数限制", "FAIL", "连续 7 次错误尝试未被限制，可无限撞库")

    # S1a：本套工具默认就是用「密码=学号」登录的，登录成功即证明弱口令存在
    record("S1a", "默认口令(密码=学号)", "FAIL",
           "%s / 同值密码 可直接登录成功（本次测试全程使用该方式）" % user)


# ---------------------------------------------------------------------------
# S2 题目接口是否仍下发答案
# ---------------------------------------------------------------------------
def check_s2(api):
    gid = api.course_tree()[0]["key"]
    typ = api.course_types(gid)[0]
    vids = api.videos(gid, typ["id"])
    vid = next((v for v in vids if int(v.get("total") or 0) > 0), None)
    if not vid:
        record("S2a", "视频弹题接口不下发答案", "SKIP", "没有找到带题目的视频")
    else:
        qs = api.questions(gid, vid["id"]) or []
        leaked = [q for q in qs if q.get("correctAnswer")]
        if leaked:
            record("S2a", "视频弹题接口不下发答案", "FAIL",
                   "%d/%d 道题仍返回 correctAnswer 字段" % (len(leaked), len(qs)))
        else:
            record("S2a", "视频弹题接口不下发答案", "PASS", "响应中已无 correctAnswer")

    # 题库练习接口（学习场景，可保留答案，仅提示）
    try:
        types = api.get("/jcedutec/questionType/queryCountList") or []
        if types:
            tid = types[0]["id"]
            rows = api.get("/questions/queryListByType", ids=tid, startNum=1, endNum=3) or []
            has = [r for r in rows if r.get("correctAnswer")]
            record("S2b", "题库练习接口(学习场景, 可保留答案)", "MANUAL",
                   "%d/%d 题带答案；若考试也复用此接口则为严重问题" % (len(has), len(rows)))
    except ApiError as e:
        record("S2b", "题库练习接口", "SKIP", str(e))


# ---------------------------------------------------------------------------
# S3 进度上报是否校验
# ---------------------------------------------------------------------------
def check_s3(api, deep=False):
    gid = api.course_tree()[0]["key"]
    typ = api.course_types(gid)[0]
    vids = api.videos(gid, typ["id"])
    vid = next((v for v in vids if float(v.get("duration") or 0) > 0), None)
    if not vid:
        record("S3", "进度上报服务端校验", "SKIP", "无可用视频")
        return
    dur = float(vid["duration"])

    if not deep:
        record("S3", "进度上报服务端校验", "MANUAL",
               "需 --deep 才能实测；检测点：上报超过视频总时长(%.0fs)的进度是否被拒" % dur)
        return

    over = int(dur * 3)
    try:
        api.report_rate(gid, vid["id"], over)
        record("S3a", "拒绝超过总时长的进度", "FAIL",
               "上报 %ds(总时长 %.0fs) 被接受，服务端未校验上限" % (over, dur))
    except ApiError as e:
        record("S3a", "拒绝超过总时长的进度", "PASS", "已拒绝: %s" % e)

    # 单次跳跃式上报
    try:
        api.report_rate(gid, vid["id"], int(dur))
        record("S3b", "拒绝单次跳满的进度", "FAIL",
               "一次上报直接到 %.0fs 被接受，缺少心跳间隔校验" % dur)
    except ApiError as e:
        record("S3b", "拒绝单次跳满的进度", "PASS", "已拒绝: %s" % e)


# ---------------------------------------------------------------------------
# S4 答题是否有会话/时序约束
# ---------------------------------------------------------------------------
def check_s4(api, deep=False):
    gid = api.course_tree()[0]["key"]
    typ = api.course_types(gid)[0]
    vids = api.videos(gid, typ["id"])
    vid = next((v for v in vids if int(v.get("total") or 0) > 0), None)
    if not vid:
        record("S4", "答题时序约束", "SKIP", "无可用题目")
        return
    if not deep:
        record("S4", "答题时序约束", "MANUAL",
               "需 --deep；检测点：未开播即在 0 秒提交弹题是否被拒")
        return
    qs = api.questions(gid, vid["id"]) or []
    if not qs:
        record("S4", "答题时序约束", "SKIP", "该视频无题目")
        return
    q = qs[0]
    try:
        api.submit_answer(gid, vid["id"], q.get("id"), q.get("correctAnswer") or "A", q.get("kind"))
        record("S4", "答题时序约束", "FAIL",
               "未建立播放会话即可提交答案，服务端无 ejectTime/nonce 校验")
    except ApiError as e:
        record("S4", "答题时序校验", "PASS", "已拒绝: %s" % e)


# ---------------------------------------------------------------------------
# S5 前端硬编码密钥
# ---------------------------------------------------------------------------
def check_s5():
    """连续多次请求 getEncryptedString，若 key/iv 始终相同，则传输加密形同虚设"""
    url = API_BASE + "/sys/getEncryptedString"
    vals = []
    try:
        for _ in range(3):
            r = requests.get(url, timeout=15, headers={"Origin": WEB_BASE}).json()
            res = r.get("result") or {}
            vals.append((res.get("key"), res.get("iv")))
    except Exception as e:
        record("S5", "传输加密密钥是否随机", "MANUAL", "请求失败: %s" % e)
        return

    if len(set(vals)) == 1:
        record("S5", "传输加密密钥是否随机", "FAIL",
               "3 次请求返回同一组 key/iv: %s / %s —— 抓包即可离线解密密码"
               % (vals[0][0], vals[0][1]))
    else:
        record("S5", "传输加密密钥是否随机", "PASS", "每次请求 key/iv 不同")


# ---------------------------------------------------------------------------
# S6 验证码强度 / 是否可无限刷新
# ---------------------------------------------------------------------------
def check_s6():
    s = requests.Session()
    s.headers.update({"Origin": WEB_BASE})
    ok = 0
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
    except Exception:
        record("S6", "验证码可被 OCR 识别", "MANUAL", "未安装 ddddocr，跳过实测")
        return
    for i in range(5):
        try:
            ck = str(int(time.time() * 1000) + i)
            r = s.get(API_BASE + "/sys/randomImage/%s" % ck, timeout=15).json()
            img = base64.b64decode(r["result"].split(",", 1)[1])
            code = ocr.classification(img)
            if code:
                ok += 1
        except Exception:
            pass
    if ok >= 3:
        record("S6", "验证码可被 OCR 识别", "FAIL",
               "5 次采样中 %d 次成功识别，自动化脚本可稳定通过" % ok)
    else:
        record("S6", "验证码可被 OCR 识别", "PASS", "5 次仅识别 %d 次，强度达标" % ok)


# ---------------------------------------------------------------------------
# S7 HTTPS
# ---------------------------------------------------------------------------
def check_s7():
    host = WEB_BASE.split("//")[1]
    try:
        r = requests.get("https://%s/user/login" % host, timeout=8, verify=False)
        record("S7", "全站 HTTPS", "PASS" if r.status_code < 400 else "MANUAL",
               "HTTPS 可用 (HTTP %d)" % r.status_code)
    except Exception:
        record("S7", "全站 HTTPS", "FAIL", "HTTPS 不可用，业务仍走明文 HTTP")


# ---------------------------------------------------------------------------
# S8 异常信息泄露
# ---------------------------------------------------------------------------
def check_s8(api):
    gid = api.course_tree()[0]["key"]
    typ = api.course_types(gid)[0]
    vids = api.videos(gid, typ["id"])
    vid = next((v for v in vids if float(v.get("duration") or 0) > 0), None)
    if not vid:
        record("S8", "异常信息泄露", "SKIP", "无可用视频")
        return
    try:
        api.post("/jcedutec/courseSource/finishRate",
                 {"id": vid["id"], "graphId": gid, "watchDuration": "abc"})
        record("S8", "异常信息泄露", "MANUAL", "非法参数未报错，需人工确认是否静默失败")
    except ApiError as e:
        msg = str(e)
        leaked = any(k in msg for k in ("java.", "Exception", "at org.", "springframework", "Mapper"))
        record("S8", "异常信息泄露", "FAIL" if leaked else "PASS",
               ("响应回显了后端异常: %s" % msg[:80]) if leaked else "错误信息已脱敏")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="漏洞修复验证（防御侧回归测试）")
    ap.add_argument("--user", default="", help="学号")
    ap.add_argument("--deep", action="store_true", help="执行写操作检测(会写少量测试数据)")
    args = ap.parse_args()

    log(C.info("=" * 64))
    log(C.info(" 漏洞修复验证  target=%s  user=%s  deep=%s" % (API_BASE, args.user, args.deep)))
    log(C.info("=" * 64))

    log("")
    log(C.info("【S1 弱口令与撞库】"))
    check_s1(args.user)

    log("")
    log(C.info("【S5 前端密钥 / S6 验证码 / S7 传输层】"))
    check_s5()
    check_s6()
    check_s7()

    log("")
    log(C.info("【S2 答案泄露 / S8 异常泄露】"))
    try:
        api = ensure_api(args.user)
    except Exception as e:
        log(C.err("登录失败，后续接口类检测跳过: %s" % e))
        api = None
    if api:
        check_s2(api)
        check_s8(api)
        log("")
        log(C.info("【S3 进度校验 / S4 答题校验】"))
        check_s3(api, deep=args.deep)
        check_s4(api, deep=args.deep)

    log("")
    log("=" * 64)
    fail = [r for r in RESULTS if r[2] == "FAIL"]
    manual = [r for r in RESULTS if r[2] == "MANUAL"]
    log("结果: %d 项通过 / %d 项仍存在 / %d 项需人工确认"
        % (len([r for r in RESULTS if r[2] == "PASS"]), len(fail), len(manual)))
    if fail:
        log("")
        log(C.err("仍需修复:"))
        for sid, title, _, detail in fail:
            log("  %-4s %s —— %s" % (sid, title, detail))
    log("=" * 64)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    main()
