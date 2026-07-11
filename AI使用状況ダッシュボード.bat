@echo off
chcp 65001 >nul
title Claude Code 使用状況ダッシュボード
cd /d "%~dp0"

rem 独立したアプリウィンドウで表示するため pythonw (コンソール無し) を優先する。
rem pywebview が未導入の場合はブラウザのアプリモードに自動フォールバックする。

where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw ai_usage_dashboard.py
  goto :eof
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw ai_usage_dashboard.py
  goto :eof
)

rem 最終手段: コンソール付きで起動
where py >nul 2>nul
if %errorlevel%==0 (
  py ai_usage_dashboard.py
) else (
  python ai_usage_dashboard.py
)

if %errorlevel% neq 0 (
  echo.
  echo [エラー] 起動に失敗しました。Python 3.11 以上がインストールされているか確認してください。
  pause
)
