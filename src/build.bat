@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  CampusLoginApp 一键重新打包脚本（build.bat）
rem  用法：双击运行，或命令行：build.bat [python.exe 完整路径]
rem  要求：装有 tkinter 的 Python 3.13+（打包环境会自动装
rem        pyinstaller 与 pillow）
rem        （默认使用 D:\python\python.exe，可用第 1 个参数
rem          或环境变量 CLA_PYTHON 覆盖）
rem  过程：重建 %TEMP%\campuslogin_build_venv -^> 装 pyinstaller/pillow -^>
rem        （缺失时）生成图标 -^> 打包 CampusLogin.exe -^> 清理中间产物
rem ============================================================

set "SRC=%~dp0"
for %%I in ("%~dp0..") do set "APPDIR=%%~fI"
set "ASSETS=%APPDIR%\assets"

set "VENV=%TEMP%\campuslogin_build_venv"
set "WORK=%TEMP%\campuslogin_build_work"

if not "%~1"=="" set "CLA_PYTHON=%~1"

if defined CLA_PYTHON (
    set "PY=%CLA_PYTHON%"
) else if exist "D:\python\python.exe" (
    set "PY=D:\python\python.exe"
) else (
    set "PY=python"
)

echo 使用 Python: %PY%
"%PY%" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3,13) else 1)" 2>nul
if errorlevel 1 (
    echo [错误] %PY% 不可用、版本低于 3.13 或缺少 tkinter，无法打包。
    echo        请安装 Python 3.13+ 并确认勾选 tcl/tk 组件，
    echo        或用参数指定完整路径：build.bat "C:\路径\python.exe"
    pause
    exit /b 1
)

echo [1/5] 重建打包虚拟环境 %VENV% ...
if exist "%VENV%" rmdir /s /q "%VENV%"
"%PY%" -m venv "%VENV%" || (echo [错误] 创建 venv 失败 & pause & exit /b 1)

echo [2/5] 安装/升级 pyinstaller 与 pillow ...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pyinstaller pillow || (echo [错误] 安装依赖失败，请检查网络 & pause & exit /b 1)

echo [3/5] 检查图标资源 assets\app.ico ...
rem 应用图标来自 assets\design\app-icon.ico（客户设计稿源图），
rem 不要用脚本从旧模板重新生成，以免覆盖客户图标。
if not exist "%ASSETS%\app.ico" (
    echo [错误] 缺少 %ASSETS%\app.ico，无法设置程序图标，中止打包。
    echo        请将 assets\design\app-icon.ico 复制为 assets\app.ico。
    pause
    exit /b 1
)
if not exist "%ASSETS%\icons" (
    echo     未找到 %ASSETS%\icons（SVG 图标变体），用 build_assets.py 生成...
    "%VENV%\Scripts\python.exe" "%SRC%build_assets.py" || (echo [错误] 图标变体生成失败 & pause & exit /b 1)
)

echo [4/5] 打包 CampusLogin.exe（带图标 / SVG 图标库）...
rem --hidden-import PIL.ImageTk：界面抗锯齿靠 ui_render.py 里的 PIL.ImageTk，
rem   而它是**惰性导入**（放在函数里，避免 daemon 模式捎带加载 tkinter）。
rem   显式声明，避免 PyInstaller 漏收导致 exe 里退回无抗锯齿的旧画法。
"%VENV%\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed --name CampusLogin --icon "%ASSETS%\app.ico" --add-data "%ASSETS%\app.png;assets" --add-data "%ASSETS%\icons;icons" --hidden-import PIL.ImageTk --distpath "%APPDIR%" --workpath "%WORK%" --specpath "%WORK%" "%SRC%app.py" || (echo [错误] 打包失败 & pause & exit /b 1)

echo [5/5] 清理中间产物 ...
if exist "%WORK%" rmdir /s /q "%WORK%"
if exist "%APPDIR%\build" rmdir /s /q "%APPDIR%\build"
if exist "%APPDIR%\dist" rmdir /s /q "%APPDIR%\dist"
if exist "%SRC%__pycache__" rmdir /s /q "%SRC%__pycache__"
del /q "%SRC%*.spec" >nul 2>&1

echo.
echo 打包完成：%APPDIR%\CampusLogin.exe
echo 提示：未签名的 exe 可能被杀毒软件/SmartScreen 误报，属正常现象。
pause
