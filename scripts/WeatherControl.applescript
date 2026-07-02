-- Weather Control (Dock app wrapper)
-- Goal: Make "/.../dist/Weather Control.app" the single entrypoint for start/restart/doctor/open.
-- It shells out to scripts/weatherctl.sh (SSOT), and tries to be resilient to repo moves.

property appName : "Weather Control"

on _trim(s)
  try
    set t to s as text
  on error
    return ""
  end try
  -- Trim leading/trailing whitespace without relying on shell input piping.
  repeat while (count of t) > 0 and my _isWhitespaceChar(character 1 of t)
    if (count of t) is 1 then return ""
    set t to text 2 thru -1 of t
  end repeat
  repeat while (count of t) > 0 and my _isWhitespaceChar(character -1 of t)
    if (count of t) is 1 then return ""
    set t to text 1 thru -2 of t
  end repeat
  return t
end _trim

on _isWhitespaceChar(ch)
  if ch is " " then return true
  if ch is (character id 9) then return true -- tab
  if ch is (character id 10) then return true -- LF
  if ch is (character id 13) then return true -- CR
  return false
end _isWhitespaceChar

on _sh(cmd)
  return do shell script cmd
end _sh

on _fileExists(p)
  try
    do shell script "/bin/test -e " & quoted form of p
    return true
  on error
    return false
  end try
end _fileExists

on _isExecutable(p)
  try
    do shell script "/bin/test -x " & quoted form of p
    return true
  on error
    return false
  end try
end _isExecutable

on _defaultRepoRoot()
  -- Fallback for this machine if the app is copied elsewhere.
  return "/Users/xiong/weather"
end _defaultRepoRoot

on _configDir()
  set base to POSIX path of (path to application support folder from user domain)
  return base & appName
end _configDir

on _repoRootConfigPath()
  return my _configDir() & "/repo_root.txt"
end _repoRootConfigPath

on _inferRepoRootFromAppLocation()
  -- If the app is at <repo>/dist/Weather Control.app, infer repo root = parent(dist).
  set appPath to POSIX path of (path to me)
  set distDir to do shell script "/usr/bin/dirname " & quoted form of appPath
  return do shell script "/bin/bash -lc " & quoted form of ("cd " & quoted form of (distDir & "/..") & " && pwd")
end _inferRepoRootFromAppLocation

on _validateRepoRoot(repoRoot)
  set ctl to repoRoot & "/scripts/weatherctl.sh"
  if my _isExecutable(ctl) then return true
  return false
end _validateRepoRoot

on _ensureRepoRoot()
  set cfgDir to my _configDir()
  try
    do shell script "/bin/mkdir -p " & quoted form of cfgDir
  end try

  -- 1) Use cached config if valid.
  set cfgPath to my _repoRootConfigPath()
  if my _fileExists(cfgPath) then
    try
      set cachedRoot to my _trim(do shell script "/bin/cat " & quoted form of cfgPath)
      if cachedRoot is not "" and my _validateRepoRoot(cachedRoot) then
        return cachedRoot
      end if
    end try
  end if

  -- 2) Infer from app location if valid.
  try
    set inferredRoot to my _inferRepoRootFromAppLocation()
    if inferredRoot is not "" and my _validateRepoRoot(inferredRoot) then
      do shell script "/bin/echo " & quoted form of inferredRoot & " > " & quoted form of cfgPath
      return inferredRoot
    end if
  end try

  -- 3) Fallback default path if valid.
  set fallbackRoot to my _defaultRepoRoot()
  if fallbackRoot is not "" and my _validateRepoRoot(fallbackRoot) then
    try
      do shell script "/bin/echo " & quoted form of fallbackRoot & " > " & quoted form of cfgPath
    end try
    return fallbackRoot
  end if

  -- 4) Ask the user.
  display dialog "找不到 scripts/weatherctl.sh。\n\n请选择项目根目录（包含 scripts/weatherctl.sh 的文件夹）。" buttons {"取消", "选择…"} default button "选择…"
  set chosen to choose folder with prompt "选择项目根目录（包含 scripts/weatherctl.sh）"
  set chosenRoot to POSIX path of chosen
  -- Remove trailing slash if any.
  set chosenRoot to my _trim(chosenRoot)
  if my _validateRepoRoot(chosenRoot) then
    try
      do shell script "/bin/echo " & quoted form of chosenRoot & " > " & quoted form of cfgPath
    end try
    return chosenRoot
  end if

  error "invalid_repo_root"
end _ensureRepoRoot

on _runCtl(ctlPath, subcmd)
  display notification ("运行: " & subcmd) with title appName
  return do shell script "/bin/bash -lc " & quoted form of (ctlPath & " " & subcmd)
end _runCtl

on _heartbeatOk(ctlPath)
  try
    set out to my _trim(my _runCtl(ctlPath, "heartbeat"))
    return out is "ok"
  on error
    return false
  end try
end _heartbeatOk

on _showMoreMenu(ctlPath)
  set items to {"启动并打开UI (up)", "一键恢复 (oneclick)", "重启服务 (restart)", "停止服务 (stop)", "打开UI (open)", "检查状态 (doctor)", "设置项目路径…"}
  set pick to choose from list items with title appName with prompt "选择操作" default items {"一键恢复 (oneclick)"}
  if pick is false then return
  set choice to item 1 of pick

  if choice is "设置项目路径…" then
    set chosen to choose folder with prompt "选择项目根目录（包含 scripts/weatherctl.sh）"
    set chosenRoot to my _trim(POSIX path of chosen)
    if not my _validateRepoRoot(chosenRoot) then
      display dialog "无效目录：找不到可执行的 scripts/weatherctl.sh" buttons {"确定"} default button "确定"
      return
    end if
    do shell script "/bin/mkdir -p " & quoted form of my _configDir()
    do shell script "/bin/echo " & quoted form of chosenRoot & " > " & quoted form of my _repoRootConfigPath()
    display notification ("已保存项目路径: " & chosenRoot) with title appName
    return
  end if

  if choice is "检查状态 (doctor)" then
    try
      set out to my _runCtl(ctlPath, "doctor")
      if (count out) > 1400 then set out to (text 1 thru 1400 of out) & "\n…(截断)"
      display dialog out buttons {"确定"} default button "确定"
    on error errMsg number errNum
      display dialog ("doctor 失败：\n" & errMsg) buttons {"确定"} default button "确定"
    end try
    return
  end if

  if choice is "启动并打开UI (up)" then
    my _runCtl(ctlPath, "up")
  else if choice is "一键恢复 (oneclick)" then
    my _runCtl(ctlPath, "oneclick")
  else if choice is "重启服务 (restart)" then
    -- restart doesn't wait for heartbeat; follow up with "up" to verify UI and open browser.
    my _runCtl(ctlPath, "restart")
    my _runCtl(ctlPath, "up")
  else if choice is "停止服务 (stop)" then
    my _runCtl(ctlPath, "stop")
  else if choice is "打开UI (open)" then
    my _runCtl(ctlPath, "open")
  end if
end _showMoreMenu

-- Main
set repoRoot to ""
set ctl to ""
try
  set repoRoot to my _ensureRepoRoot()
  set ctl to repoRoot & "/scripts/weatherctl.sh"

  -- Quick buttons: behave like a start/restart button, with a "more" menu.
  set isUp to my _heartbeatOk(ctl)
  set defaultBtn to "启动/恢复"
  if isUp then set defaultBtn to "重启"

  set msg to "项目路径：\n" & repoRoot
  if isUp then
    set msg to msg & "\n\n状态：UI 心跳正常。"
  else
    set msg to msg & "\n\n状态：UI 心跳未就绪。"
  end if

  display dialog msg with title appName buttons {"更多…", "重启", "启动/恢复"} default button defaultBtn
  set btn to button returned of result

  if btn is "更多…" then
    my _showMoreMenu(ctl)
  else if btn is "重启" then
    my _runCtl(ctl, "restart")
    my _runCtl(ctl, "up")
  else
    my _runCtl(ctl, "oneclick")
  end if

on error errMsg number errNum
  display dialog "操作失败。\n\n可手动执行：\n- scripts/weatherctl.sh oneclick\n- scripts/weatherctl.sh doctor\n\n错误：" & errMsg buttons {"确定"} default button "确定"
end try
