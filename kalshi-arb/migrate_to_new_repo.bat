@echo off
REM ------------------------------------------------------------------
REM  Migrate kalshi-arb from a subdirectory of the main Kalshi repo
REM  into its own standalone GitHub repo at
REM  github.com/Mikeytickets17/kalshi-arb.
REM
REM  Run this ONCE after the empty kalshi-arb repo has been created.
REM  It uses git subtree to preserve full history.
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0\.."

echo.
echo ======================================================
echo  Migrating kalshi-arb to standalone repo
echo ======================================================
echo.

REM -- Make sure we're in the parent Kalshi repo --
if not exist "kalshi-arb\pyproject.toml" (
    echo [ERROR] Expected kalshi-arb\pyproject.toml. Are you in the right folder?
    pause
    exit /b 1
)

REM -- Create the standalone-split branch --
echo Splitting subdirectory into standalone branch...
git subtree split --prefix=kalshi-arb -b kalshi-arb-standalone
if errorlevel 1 (
    echo [ERROR] git subtree split failed.
    pause
    exit /b 1
)

REM -- Add the new remote --
git remote remove kalshi-arb-origin 2>nul
git remote add kalshi-arb-origin https://github.com/Mikeytickets17/kalshi-arb.git

REM -- Push to the new repo's main branch --
echo Pushing to github.com/Mikeytickets17/kalshi-arb ...
git push kalshi-arb-origin kalshi-arb-standalone:main
if errorlevel 1 (
    echo [ERROR] Push failed. Check credentials / repo access.
    pause
    exit /b 1
)

REM -- Clean up --
git branch -D kalshi-arb-standalone 2>nul
git remote remove kalshi-arb-origin 2>nul

echo.
echo ======================================================
echo  Migration complete.
echo  github.com/Mikeytickets17/kalshi-arb now has the code
echo  with full history.
echo ======================================================
pause
