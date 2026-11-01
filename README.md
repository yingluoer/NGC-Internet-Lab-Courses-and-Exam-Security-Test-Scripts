# 实验室安全学习平台 · 自动化学习工具

> ⚠️ **免责声明**：本项目**仅用于网页安全性测试与学习研究**，请勿用于任何非法用途或盈利。
> 使用本工具产生的任何后果由使用者自行承担，开发者概不负责。

针对 **南京工程学院 实验室安全育人智慧学习系统**（http://202.119.160.23:83）的自动化学习工具。
**仅限南京工程学院使用，其他学校 / 平台不可用。**

## 两个版本

| 文件 | 形态 | 说明 |
|---|---|---|
| `dist/SafetyTerminal.exe` | **Web 控制台**（198MB） | 双击启动本地服务并自动开浏览器，图形界面操作 |
| `dist/SafetyLearn.exe` | 命令行（196MB） | 双击进菜单，或带参数执行 |

推荐用 `SafetyTerminal.exe`。

---

## 零、Web 控制台

双击后会自动打开 `http://127.0.0.1:8769/`，界面是工业终端风格：

```
┌ LAB SAFETY TERMINAL ──────────────── LINKED  NODE <学号>  14:52 ┐
├──────────┬───────────────────────────────────────────────────────┤
│ 01 终端接入│ 02 课程进度矩阵                                       │
│ [学号]    │  ┌安全概述 4/4 8/8┐ ┌水电安全 10/10 22/22┐ ...        │
│ CONNECT ▸ │  ─────────────────────────────────────────            │
│          │  TOTAL VIDEO 60   TOTAL QUIZ 121   COMPLETION 100%     │
│ 已保存终端 │ 03 指令                                               │
│ <学号>    │  [同步进度] [执行学习] [题库导出] [模拟测试]           │
├──────────┴───────────────────────────────────────────────────────┤
│ 04 SYSTEM LOG   14:52:13 接入终端 <学号> ...                    │
└──────────────────────────────────────────────────────────────────┘
```

功能：
- 输入学号点 `CONNECT`，自动识别验证码完成登录
- 左侧列出已登录的终端，点击即可切换
- 卡片显示每门课的视频/习题进度，全部完成会标记 `CLEAR`
- `快速测试` 10 题随机，练手感
- **`全真模考`** 完全按真实考试结构出题：50 题 / 30 分钟倒计时 / 90 分及格，
  知识点分布也是照实考的 5 个模块各 10 题（通识·水电·消防·应急·机电），
  题型只出判断题和单选题（和真实试卷一致）。交卷后按 90 分线判 `QUALIFIED / FAILED`，
  并列出全部错题的正确答案。

> 全真模考是把你的真实考试"预演"到能稳过 90 分为止。正式考试仍需本人签名、本人作答。

端口被占用会自动顺延（8769 起）。

---

## 一、能做什么

| 功能 | 说明 |
|---|---|
| 自动登录 | 图形验证码用 OCR 自动识别（已打包进 EXE），识别错自动重试 |
| 多账号 | 可批量登录、切换、批量查进度 |
| 高倍速学习 | 并发上报，60 个视频 + 121 道题实测约 **10 秒**（单视频 0.41 秒 × 6 并发） |
| 弹题满分 | 视频弹题的正确答案由题目接口下发，直接提交，零错题 |
| 断点续传 | 已完成的视频、已答过的题自动跳过，可反复运行 |
| 题库 | 4288 题导出为 CSV/JSON，可离线复习 |
| 模拟测试 | 本地随机组卷、终端答题、自动判分、错题解析 |



---

## 二、环境准备

```bash
# 1. 安装依赖（Python 3.10+）
pip install requests playwright pycryptodome ddddocr

# 2. 安装浏览器驱动（本机已装 Edge/Chrome 可跳过）
python -m playwright install chromium
```

> 登录环节需要真实浏览器（用来自动完成密码加密），脚本会优先调用本机 Edge，其次 Chrome，最后用 Playwright 自带的 Chromium。

---

## 三、用法

```bash
# 登录（保存会话到 session.json，有效期约 4 小时）
python nk_auto.py login

# 查看学习进度
python nk_auto.py status

# 先预演，看清楚会做什么（不写入任何数据）
python nk_auto.py learn --dry-run

# 正式学习全部课程
python nk_auto.py learn

# 只学某一门
python nk_auto.py learn --only 水电安全

# 登录时想看浏览器窗口（排查问题用）
python nk_auto.py login --headed
```

### 参数

| 参数 | 作用 |
|---|---|
| `--user` | 学号（密码默认与学号相同） |
| `--only` | 只处理课程名包含该关键字的课程，如 `--only 消防` |
| `--dry-run` | 预演模式，只打印计划，不调用写接口 |
| `--relogin` | 强制重新登录，忽略本地缓存 |
| `--headed` | 登录时显示浏览器窗口 |

---

## 四、执行效果

```
课程         视频进度           习题进度           错题
--------------------------------------------------------------
✔ 安全概述      4/4            8/8            0
✔ 水电安全      10/10          22/22          0
✔ 消防安全      9/9            16/16          0
✔ 特种设备      4/4            8/8            0
✔ 应急处置      18/18          37/37          0
✔ 机电安全      15/15          30/30          0
--------------------------------------------------------------
合计: 视频 60/60 · 习题 121/121
```

---

## 五、原理说明（便于日后维护）

| 环节 | 接口 |
|---|---|
| 验证码 | `GET /sys/randomImage/{checkKey}?_t=ts` → base64 图片 |
| 登录 | `POST /sys/userLogin` {username, password(AES), captcha, checkKey, remember_me} |
| 课程树 | `GET /jcedutec/knowledgeGraph/queryGraphTree` |
| 课程分类 | `GET /jcedutec/knowledgeGraph/queryCourseTypeById?id={graphId}&courseType=1` |
| 视频列表 | `GET /jcedutec/home/detailByTypeAndGraph?graphId=&id={typeId}` |
| 计数 | `POST /jcedutec/courseSource/updateVisits` {id, graphId} |
| 进度上报 | `POST /jcedutec/courseSource/finishRate` {id, graphId, watchDuration(整数秒)} |
| 完成标记 | `POST /jcedutec/courseSource/finish` {id, graphId} |
| 取弹题 | `GET /jcedutec/courseSource/queryCourseQuestionRelaByMainId?id=&graphId=` |
| 交答案 | `POST /jcedutec/courseSource/submitAnswer` {questionId, id, graphId, option} |

几点关键细节：

1. **密码不是明文传输**，前端先用 `GET /sys/getEncryptedString` 拿 key/iv，再用 AES-CBC 加密。本工具不重写这段逻辑，交给浏览器页面自己算，最稳。
2. **`watchDuration` 必须是整数**，传小数服务端会抛 `NumberFormatException`。
3. **页面本身有防快进**：播放时若 `currentTime` 单次跳跃超过 3 秒会被拉回。本工具不走播放器，直接按比例分段上报进度，绕开了这个限制。
4. **多选题** `option` 要传数组 `["A","B"]`，单选/判断题传字符串 `"A"`。
5. **`submitAnswer` 的 `questionId` 要传题目列表里的 `id`（关联表主键），不是 `questionId` 字段**。两者常常相同，但部分题目不同；传错会报「查询习题网络超时」（这个报错是业务异常，不是真的网络问题）。
6. **题目接口直接返回 `correctAnswer`**，因此弹题必然满分。答题接口对重复提交做了去重，不会重复计数。

---

## 六、注意事项

- 只在**校内网**或能访问 `202.119.160.23` 的网络下运行。
- 会话（`session.json`）有效期约 4 小时，过期会自动重新登录。
- 学习记录会真实写入账号，如需保留原始进度请勿在他人账号上运行。
- `--dry-run` 可以安全地预览，不会产生任何数据。
- 如果 OCR 连续识别失败，用 `--headed` 观察，或临时把验证码图片存下来人工确认。

---

## 七、题库与模拟测试

```bash
# 导出题库（20 个分类 / 4288 题，约 20 秒）
python question_bank.py export

# 题库统计
python question_bank.py stat

# 模拟测试：随机抽 10 题，终端作答，自动判分
python question_bank.py mock

# 只练某几类
python question_bank.py mock --num 20 --types 水电安全,消防安全

# 固定随机种子（同一套卷复现）
python question_bank.py mock --seed 42
```

导出结果：`question_bank.json`（结构化）和 `question_bank.csv`（Excel 可直接打开）。
模拟测试交卷后会列出所有错题的题干、选项、你的答案与正确答案。

