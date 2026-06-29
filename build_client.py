#!/usr/bin/env python3
"""
HermesUnified 客户端 — PyInstaller 打包配置

用法：
    pip install pyinstaller Pillow
    python build_client.py

输出：
    dist/HermesUnified.exe
"""
import PyInstaller.__main__
import os, sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── 文件清单 ──
# 所有 .py 文件都需要通过 --add-data 包含进 exe
# 格式: "源路径;目标目录"
files = [
    # 主入口 + 核心模块
    ("hermes_unified.py",   "."),      # 主入口
    ("windows_agent_v4.py", "."),      # Agent 逻辑
    ("config.py",           "."),      # 共享配置
    ("hermes_packet.py",    "."),      # 协议包
    ("updater.py",          "."),      # 外部更新器
    
    # TLS 证书生成工具（可选，供调试）
    ("gen_tls_certs.py",    "."),
]

data_files = []
for src, dst in files:
    if os.path.exists(src):
        data_files.append(f"--add-data={src};{dst}")

# ── PyInstaller 参数 ──
args = [
    "hermes_unified.py",           # 主入口文件
    "--name=HermesUnified",        # 输出文件名
    "--noconsole",                 # 隐藏控制台窗口（GUI 模式）
    "--onefile",                   # 单文件 exe
    "--clean",                     # 清理临时文件
    "--noconfirm",                 # 不询问覆盖
    
    # 隐藏导入（PyInstaller 可能检测不到的）
    "--hidden-import=PIL",
    "--hidden-import=PIL.Image",
    
    # 数据文件
    *data_files,
]

# 如果有图标，取消注释下面这行：
# args.append("--icon=icon.ico")

print("PyInstaller 参数:")
for a in args:
    print(f"  {a}")

PyInstaller.__main__.run(args)
print("\n✅ 打包完成: dist/HermesUnified.exe")
