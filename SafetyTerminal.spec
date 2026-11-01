# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: SafetyTerminal.exe (基于 lab_safety_bot_v2) — onefile 单文件版
入口 server.py, 含 nk_auto/question_bank + static 前端 + question_bank.json 题库
关键: ddddocr 的 .onnx 模型文件和 playwright 的 driver 必须作为 datas 显式打入
onefile 模式: a.binaries/a.zipfiles/a.datas 全部打进 exe, 运行时解压到 _MEIPASS
"""
import os
import ddddocr as _ddddocr
import playwright as _playwright

# 产物名(打包时用环境变量 SL_NAME 指定, 用于 V/Y 两版本)
APP_NAME = os.environ.get('SL_NAME', 'SafetyTerminal')

ROOT = os.path.abspath('.')
_DDDDOCR_DIR = os.path.dirname(os.path.abspath(_ddddocr.__file__))
_PLAYWRIGHT_DIR = os.path.dirname(os.path.abspath(_playwright.__file__))
# playwright 自带浏览器缓存目录(Chromium headless shell + ffmpeg), 打进后不依赖本机 Edge/Chrome
_PW_BROWSERS = os.path.join(os.environ.get('LOCALAPPDATA',
                            os.path.expanduser('~/AppData/Local')), 'ms-playwright')

a = Analysis(
    ['server.py'],
    pathex=[ROOT],
    binaries=[],
    datas=[
        ('static', 'static'),
        ('question_bank.json', '.'),
        # ddddocr 模型文件(common.onnx 等)必须打入, 否则打包后 OCR 不可用走手输
        (_DDDDOCR_DIR, 'ddddocr'),
        # playwright 的 driver/scripts 必须打入, 否则打包后无法启动浏览器
        (_PLAYWRIGHT_DIR, 'playwright'),
        # 自带 Chromium headless shell(登录默认 headless 模式) + ffmpeg
        (os.path.join(_PW_BROWSERS, 'chromium_headless_shell-1248'),
         'ms-playwright/chromium_headless_shell-1248'),
        (os.path.join(_PW_BROWSERS, 'ffmpeg-1013'), 'ms-playwright/ffmpeg-1013'),
    ],
    hiddenimports=[
        'nk_auto', 'question_bank', 'feature_flags',
        'flask',
        'Crypto.Cipher.AES', 'Crypto.Util.Padding',
        'ddddocr', 'onnxruntime',
        'playwright',
        'PIL',
    ],
    excludes=['pytest', 'unittest', 'doctest'],
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME,
    icon='app_icon.ico',
    version='version.txt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
