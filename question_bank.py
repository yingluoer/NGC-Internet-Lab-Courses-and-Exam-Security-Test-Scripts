# -*- coding: utf-8 -*-
"""
题库导出与模拟测试
==================
用途:
  1. export  把「题库练习」里开放的题目导到本地, 方便离线复习
  2. mock    用本地题库随机组卷, 在终端里做模拟测试并判分
  3. stat    查看题库规模

注意:
  本工具只做「复习」和「自测」, 不参与平台的正式考试。
  平台正式考试前需要阅读《考生承诺书》并手写签名, 签名和答题都应由本人完成。
"""
import argparse
import csv
import json
import os
import random
import sys
import time

from nk_auto import ApiError, C, _app_dir, ensure_api, log

BASE_DIR = _app_dir()
BANK_JSON = os.path.join(BASE_DIR, "question_bank.json")
BANK_CSV = os.path.join(BASE_DIR, "question_bank.csv")

BATCH = 50          # 每次拉取题量
GAP = 0.15          # 请求间隔


# ----------------------------------------------------------------------------
# 导出
# ----------------------------------------------------------------------------
def export_bank(api, force=False):
    if os.path.exists(BANK_JSON) and not force:
        log(C.warn("题库已存在 (%s), 如需重新拉取请加 --force" % BANK_JSON))
        return load_bank()

    types = api.get("/jcedutec/questionType/queryCountList") or []
    log(C.info("题库共 %d 个分类" % len(types)))

    all_items = []
    for t in types:
        tid, tname = t["id"], t["type"]
        n = int(t.get("questionNum") or 0)
        if n <= 0:
            continue
        got = 0
        for start in range(1, n + 1, BATCH):
            end = min(start + BATCH - 1, n)
            try:
                rows = api.get("/questions/queryListByType",
                               ids=tid, startNum=start, endNum=end) or []
            except ApiError as e:
                log(C.warn("  ! %s [%d-%d] 拉取失败: %s" % (tname, start, end, e)))
                continue
            for r in rows:
                all_items.append({
                    "id": r.get("id"),
                    "typeId": tid,
                    "type": r.get("type_dictText") or tname,
                    "kind": r.get("kind"),
                    "kindName": r.get("kind_dictText"),
                    "stem": (r.get("stem") or "").strip(),
                    "A": r.get("optiona"), "B": r.get("optionb"),
                    "C": r.get("optionc"), "D": r.get("optiond"),
                    "answer": (r.get("correctAnswer") or "").strip(),
                    "degree": r.get("degree_dictText"),
                    "isPic": r.get("isPic"),
                })
            got += len(rows)
            time.sleep(GAP)
        log(C.dim("  · %-6s %d/%d" % (tname, got, n)))

    with open(BANK_JSON, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=1)
    with open(BANK_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "分类", "题型", "题干", "A", "B", "C", "D", "正确答案", "难度"])
        for i, q in enumerate(all_items, 1):
            w.writerow([i, q["type"], q["kindName"], q["stem"],
                        q["A"] or "", q["B"] or "", q["C"] or "", q["D"] or "",
                        q["answer"], q["degree"]])
    log(C.ok("已导出 %d 题 -> question_bank.json / question_bank.csv" % len(all_items)))
    return all_items


def _res_dir():
    """打包后随 EXE 一起带的资源目录"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", BASE_DIR)
    return BASE_DIR


def load_bank():
    # 优先用用户目录下的, 其次用打包内置的
    for p in (BANK_JSON, os.path.join(_res_dir(), "question_bank.json")):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
    return []


# ----------------------------------------------------------------------------
# 模拟测试
# ----------------------------------------------------------------------------
def ask(q, idx, total):
    """展示一题并收集作答, 返回选项字符串(如 'A' 或 'A,C')"""
    log("")
    log(C.info("第 %d/%d 题  [%s·%s]" % (idx, total, q["type"], q["kindName"])))
    log(C.warn("  " + q["stem"]))
    for k in ("A", "B", "C", "D"):
        if q.get(k):
            log("    %s. %s" % (k, q[k]))
    hint = "输入选项字母(多选用逗号分隔, 如 A,C), 直接回车跳过: "
    while True:
        try:
            raw = input(hint).strip().upper().replace("，", ",").replace(" ", "")
        except EOFError:
            return ""
        if raw == "":
            return ""
        letters = [x for x in raw.replace(",", "")]
        if all(x in "ABCD" for x in letters) and letters:
            return ",".join(sorted(set(letters)))
        log(C.warn("  输入无效, 请重新输入"))


def mock_exam(bank, num=10, types=None, auto=False, seed=None):
    pool = bank
    if types:
        keys = [t.strip() for t in types.split(",") if t.strip()]
        pool = [q for q in bank if any(k in (q.get("type") or "") for k in keys)]
        if not pool:
            log(C.err("没有匹配的题目, 请检查 --types"))
            return
        log(C.info("题库筛选: %s -> %d 题" % (types, len(pool))))

    if seed is not None:
        random.seed(seed)
    num = min(num, len(pool))
    paper = random.sample(pool, num)
    log(C.info("模拟试卷: %d 题 (从 %d 题中随机抽取)" % (num, len(pool))))
    if auto:
        log(C.warn("自动模式: 随机作答, 仅用于验证流程"))

    t0 = time.time()
    answers = []
    for i, q in enumerate(paper, 1):
        if auto:
            ans = random.choice(["A", "B", "C", "D"])
            log(C.dim("  第%d题 自动作答 %s" % (i, ans)))
            answers.append(ans)
        else:
            answers.append(ask(q, i, num))
    cost = int(time.time() - t0)

    # 判分
    log("")
    log("=" * 60)
    right = 0
    wrong = []
    for q, a in zip(paper, answers):
        std = ",".join(sorted(q["answer"].split(",")))
        if a and set(a.split(",")) == set(std.split(",")):
            right += 1
        else:
            wrong.append((q, a, std))
    score = round(right / num * 100, 1)
    log("答题 %d 题, 正确 %d 题, 得分 %s, 用时 %d 秒"
        % (num, right, C.ok(str(score)) if score >= 90 else C.warn(str(score)), cost))
    if not wrong:
        log(C.ok("全部正确, 满分!"))
    else:
        log(C.warn("错题 %d 道:" % len(wrong)))
        for q, a, std in wrong:
            log("  · " + q["stem"])
            opts = "  ".join("%s.%s" % (k, q[k]) for k in "ABCD" if q.get(k))
            log(C.dim("    " + opts))
            log("    你的答案: %s   正确答案: %s" % (a or "(未答)", std))
    log("=" * 60)


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="题库导出 / 本地模拟测试")
    sub = ap.add_subparsers(dest="cmd")

    p1 = sub.add_parser("export", help="导出题库到本地")
    p1.add_argument("--force", action="store_true", help="重新拉取")
    p1.add_argument("--user", default="")

    p2 = sub.add_parser("mock", help="本地模拟测试")
    p2.add_argument("--num", type=int, default=10, help="题量, 默认 10")
    p2.add_argument("--types", default=None, help="限定分类, 逗号分隔, 如 水电安全,消防安全")
    p2.add_argument("--auto", action="store_true", help="自动随机作答(验证流程用)")
    p2.add_argument("--seed", type=int, default=None, help="固定随机种子")

    p3 = sub.add_parser("stat", help="题库统计")

    args = ap.parse_args()

    if args.cmd == "export":
        try:
            api = ensure_api(args.user)
            export_bank(api, force=args.force)
        except Exception as e:
            log(C.err("导出失败: %s" % e))
            sys.exit(1)

    elif args.cmd == "mock":
        bank = load_bank()
        if not bank:
            log(C.err("本地还没有题库, 先执行: python question_bank.py export"))
            sys.exit(1)
        mock_exam(bank, num=args.num, types=args.types, auto=args.auto, seed=args.seed)

    elif args.cmd == "stat":
        bank = load_bank()
        if not bank:
            log(C.err("本地还没有题库, 先执行: python question_bank.py export"))
            sys.exit(1)
        from collections import Counter
        log(C.info("题库总量: %d 题" % len(bank)))
        for name, n in Counter(q["type"] for q in bank).most_common():
            log("  %-8s %4d" % (name, n))
        log("")
        for name, n in Counter(q.get("kindName") or "未知" for q in bank).most_common():
            log("  %-8s %4d" % (name, n))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
