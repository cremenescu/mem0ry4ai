# mem0ry4ai - Windows install and uninstall, with a graphical front end.
#
# Everything here is something you could type yourself; the window exists so you can see what is
# about to happen and watch it happen, not to hide it. Every command is echoed before it runs.
#
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1
#
# Administrator rights are never needed: packages install per-user via winget, the clone goes in
# your profile, and the hooks are written to your own Claude Code settings.
#
# On uninstall, the memories are kept. See the Uninstall tab.

#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Silent,                 # install with defaults, no window
    [switch]$SilentUninstall,        # uninstall, no window, memories always kept
    [string]$InstallPath = "$env:USERPROFILE\mem0ry4ai",
    [string]$DataPath    = "$env:USERPROFILE\.mem0ry4ai"
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:RepoUrl = 'https://github.com/cremenescu/mem0ry4ai.git'
$script:LogBox  = $null

function Add-Log {
    param([string]$Message, [string]$Level = 'info')
    $prefix = switch ($Level) {
        'ok'    { '  OK   ' }
        'warn'  { '  WARN ' }
        'error' { '  FAIL ' }
        'cmd'   { '  >    ' }
        default { '       ' }
    }
    $line = "$prefix$Message"
    if ($script:LogBox) {
        $script:LogBox.AppendText($line + [Environment]::NewLine)
        $script:LogBox.SelectionStart = $script:LogBox.TextLength
        $script:LogBox.ScrollToCaret()
        [System.Windows.Forms.Application]::DoEvents()
    } else {
        Write-Information $line -InformationAction Continue
    }
}

function Invoke-Logged {
    <#
      Run a command, show it first, and report whether it worked.

      The echo is not decoration: an installer that says "done" without showing what it did is
      indistinguishable from one that did nothing, and you cannot repeat by hand a step you never
      saw. Output is captured and replayed into the log rather than thrown away.
    #>
    param([string]$File, [string[]]$Arguments, [string]$WorkingDirectory = $PWD)

    Add-Log "$File $($Arguments -join ' ')" 'cmd'
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName  = $File
    $psi.Arguments = ($Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true

    try { $p = [System.Diagnostics.Process]::Start($psi) }
    catch { Add-Log "could not start ${File}: $($_.Exception.Message)" 'error'; return $false }

    $out = $p.StandardOutput.ReadToEnd()
    $err = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    foreach ($l in ($out + $err) -split "`r?`n") { if ($l.Trim()) { Add-Log "    $l" } }
    return ($p.ExitCode -eq 0)
}

function Resolve-Tool {
    param([string]$Name)
    $c = Get-Command $Name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

function Get-PythonExe {
    <#
      Prefer the `py` launcher: it is what a Windows Python install actually provides and it
      survives a version upgrade. Reject the Microsoft Store stub - it sits on PATH by default and
      does nothing but open the Store, producing a baffling "Python is installed and nothing works".
    #>
    $py = Resolve-Tool 'py'
    if ($py) { return $py }
    $python = Resolve-Tool 'python'
    if ($python -and $python -notlike '*WindowsApps*') { return $python }
    return $null
}

function Test-Prerequisite {
    return @{ Python = (Get-PythonExe); Git = (Resolve-Tool 'git'); Winget = (Resolve-Tool 'winget') }
}

function Install-Prerequisite {
    param([string]$WingetId, [string]$Label)
    if (-not (Resolve-Tool 'winget')) {
        Add-Log "$Label is missing and winget is unavailable - install $Label yourself, then run this again" 'error'
        return $false
    }
    Add-Log "installing $Label via winget (per-user, no admin)" 'info'
    $ok = Invoke-Logged 'winget' @('install', '--id', $WingetId, '--scope', 'user',
                                   '--accept-package-agreements', '--accept-source-agreements',
                                   '--silent', '--disable-interactivity')
    if ($ok) {
        # winget updates the stored PATH, not this already-running process's copy of it.
        $env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('PATH', 'User')
        Add-Log "$Label installed" 'ok'
    } else {
        Add-Log "$Label install failed - see the output above" 'error'
    }
    return $ok
}

function Set-GitBashPath {
    <#
      Claude Code on Windows needs a git-bash to run shell commands and does not find one on its
      own. Without CLAUDE_CODE_GIT_BASH_PATH the hooks register fine and then never fire, which
      looks exactly like mem0ry4ai being broken. Set for the current user only.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    if ([Environment]::GetEnvironmentVariable('CLAUDE_CODE_GIT_BASH_PATH', 'User')) {
        Add-Log 'CLAUDE_CODE_GIT_BASH_PATH already set - left alone' 'ok'
        return
    }
    $bash = @("$env:ProgramFiles\Git\bin\bash.exe",
              "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
              "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $bash) {
        Add-Log 'git-bash not found - if hooks never fire, set CLAUDE_CODE_GIT_BASH_PATH by hand' 'warn'
        return
    }
    if ($PSCmdlet.ShouldProcess('CLAUDE_CODE_GIT_BASH_PATH', 'set user environment variable')) {
        [Environment]::SetEnvironmentVariable('CLAUDE_CODE_GIT_BASH_PATH', $bash, 'User')
        Add-Log "CLAUDE_CODE_GIT_BASH_PATH set to $bash (restart Claude Code to pick it up)" 'ok'
    }
}

# ------------------------------------------------------------------------------------ install
function Invoke-Install {
    param([string]$Target, [string]$Data, [bool]$DoHooks, [bool]$DoStart, [bool]$SeparateData)

    Add-Log '--- checking what is already here ---'
    $pre = Test-Prerequisite

    if (-not $pre.Git) {
        Add-Log 'git: missing' 'warn'
        if (-not (Install-Prerequisite 'Git.Git' 'Git')) { return $false }
    } else { Add-Log "git: $($pre.Git)" 'ok' }

    if (-not $pre.Python) {
        Add-Log 'python: missing' 'warn'
        if (-not (Install-Prerequisite 'Python.Python.3.12' 'Python')) { return $false }
    } else { Add-Log "python: $($pre.Python)" 'ok' }

    $python = Get-PythonExe
    $git    = Resolve-Tool 'git'
    if (-not $python -or -not $git) {
        Add-Log 'python or git still not on PATH - open a NEW terminal and run this again' 'error'
        return $false
    }

    Add-Log ''
    Add-Log '--- getting the code ---'
    if (Test-Path (Join-Path $Target '.git')) {
        Add-Log "already a clone at $Target - updating instead of re-cloning" 'info'
        if (-not (Invoke-Logged $git @('-C', $Target, 'pull', '--ff-only'))) {
            Add-Log 'update failed; the existing clone was left untouched' 'warn'
        }
    } else {
        if ((Test-Path $Target) -and (Get-ChildItem $Target -Force | Select-Object -First 1)) {
            Add-Log "$Target exists, is not empty and is not a clone - pick another folder" 'error'
            return $false
        }
        if (-not (Invoke-Logged $git @('clone', '--depth', '1', $script:RepoUrl, $Target))) { return $false }
    }
    Add-Log "code in $Target" 'ok'

    if ($SeparateData) {
        # Memories outside the clone: updating the code can never touch them, and a clone you
        # delete is not a store you lost.
        New-Item -ItemType Directory -Force -Path $Data | Out-Null
        [Environment]::SetEnvironmentVariable('MEM_DATA_DIR', $Data, 'User')
        $env:MEM_DATA_DIR = $Data
        Add-Log "memories will live in $Data (MEM_DATA_DIR set for your user)" 'ok'
    } else {
        Add-Log "memories will live inside the clone, in $Target\store" 'ok'
    }

    Add-Log ''
    Add-Log '--- first memory, which also creates the store and its git history ---'
    $null = Invoke-Logged $python @('mem.py', 'add', '--type', 'fact', '--scope', 'global',
        '--summary', 'mem0ry4ai installed on this machine',
        '--body', 'Installed with install-windows.ps1. Delete this once you have real memories.') $Target

    if ($DoHooks) {
        Add-Log ''
        Add-Log '--- Claude Code hooks ---'
        if (Invoke-Logged $python @('hooks\install.py', '--target', 'user') $Target) {
            Add-Log 'hooks registered - restart Claude Code (or /clear) to load them' 'ok'
        } else {
            Add-Log 'hook install failed - mem0ry4ai still works from the CLI and the web UI' 'warn'
        }
        Set-GitBashPath
    }

    if ($DoStart) {
        Add-Log ''
        Add-Log '--- web UI ---'
        Start-Process -FilePath $python -ArgumentList 'mem.py', 'serve' -WorkingDirectory $Target -WindowStyle Hidden
        Start-Sleep -Seconds 3
        try {
            $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8841/' -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) {
                Add-Log 'web UI answering on http://127.0.0.1:8841/' 'ok'
                Start-Process 'http://127.0.0.1:8841/'
            }
        } catch {
            Add-Log 'web UI did not answer yet - start it by hand with: py mem.py serve' 'warn'
        }
    }

    Add-Log ''
    Add-Log '--- done ---' 'ok'
    Add-Log "From a terminal:  cd $Target  then  py mem.py list"
    return $true
}

# ---------------------------------------------------------------------------------- uninstall
function Invoke-Uninstall {
    <#
      Delegates to uninstall.py so Windows and Unix undo the same things in the same order, and
      there is one place to fix when the list changes. Memories are removed only when the caller
      explicitly asks - and uninstall.py asks again, in its own words.
    #>
    param([string]$Target, [bool]$DeleteMemories, [bool]$Preview)

    $python = Get-PythonExe
    if (-not $python) {
        Add-Log 'python not found - cannot run the uninstaller' 'error'
        return $false
    }
    $script = Join-Path $Target 'uninstall.py'
    if (-not (Test-Path $script)) {
        Add-Log "uninstall.py not found in $Target - point the folder at your install" 'error'
        return $false
    }

    # Not $args: that is a PowerShell automatic variable, and assigning to it inside a function
    # shadows the caller's arguments.
    $pyArgs = @('uninstall.py')
    if (-not $Preview) { $pyArgs += '--yes' }
    if ($DeleteMemories) { $pyArgs += '--delete-memories' }

    Add-Log $(if ($Preview) { '--- preview: nothing will be changed ---' } else { '--- removing ---' })
    $ok = Invoke-Logged $python $pyArgs $Target

    if (-not $Preview) {
        if ([Environment]::GetEnvironmentVariable('MEM_DATA_DIR', 'User')) {
            [Environment]::SetEnvironmentVariable('MEM_DATA_DIR', $null, 'User')
            Add-Log 'MEM_DATA_DIR cleared from your user environment' 'ok'
        }
        # CLAUDE_CODE_GIT_BASH_PATH is deliberately left: Claude Code itself may now depend on it,
        # and removing a setting that fixes someone else's tool is not ours to do.
        Add-Log 'CLAUDE_CODE_GIT_BASH_PATH left in place (Claude Code may rely on it)' 'info'
        Add-Log ''
        Add-Log "The code folder is still at $Target - delete it yourself when ready." 'info'
    }
    return $ok
}

# -------------------------------------------------------------------------------- silent modes
if ($Silent) {
    exit ([int](-not (Invoke-Install -Target $InstallPath -Data $DataPath `
                                     -DoHooks $true -DoStart $false -SeparateData $true)))
}
if ($SilentUninstall) {
    exit ([int](-not (Invoke-Uninstall -Target $InstallPath -DeleteMemories $false -Preview $false)))
}

# -------------------------------------------------------------------------------------- window
$form                 = New-Object System.Windows.Forms.Form
$form.Text            = 'mem0ry4ai'
$form.Size            = New-Object System.Drawing.Size(770, 660)
$form.StartPosition   = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox     = $false
$form.Font            = New-Object System.Drawing.Font('Segoe UI', 9)

$tabs          = New-Object System.Windows.Forms.TabControl
$tabs.Location = New-Object System.Drawing.Point(12, 12)
$tabs.Size     = New-Object System.Drawing.Size(730, 250)
$form.Controls.Add($tabs)

# --- install tab
$tabInstall      = New-Object System.Windows.Forms.TabPage
$tabInstall.Text = '  Install  '
$tabs.TabPages.Add($tabInstall)

$sub          = New-Object System.Windows.Forms.Label
$sub.Text     = 'Persistent memory for coding agents. Needs Python and git - both are installed for you if missing.'
$sub.Location = New-Object System.Drawing.Point(14, 12)
$sub.Size     = New-Object System.Drawing.Size(690, 20)
$sub.ForeColor = [System.Drawing.Color]::FromArgb(90, 90, 90)
$tabInstall.Controls.Add($sub)

$lblPath          = New-Object System.Windows.Forms.Label
$lblPath.Text     = 'Install the code in'
$lblPath.Location = New-Object System.Drawing.Point(14, 44)
$lblPath.Size     = New-Object System.Drawing.Size(200, 20)
$tabInstall.Controls.Add($lblPath)

$txtPath          = New-Object System.Windows.Forms.TextBox
$txtPath.Text     = $InstallPath
$txtPath.Location = New-Object System.Drawing.Point(14, 64)
$txtPath.Size     = New-Object System.Drawing.Size(590, 24)
$tabInstall.Controls.Add($txtPath)

$btnBrowse          = New-Object System.Windows.Forms.Button
$btnBrowse.Text     = 'Browse'
$btnBrowse.Location = New-Object System.Drawing.Point(612, 63)
$btnBrowse.Size     = New-Object System.Drawing.Size(90, 26)
$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    if ($dlg.ShowDialog() -eq 'OK') { $txtPath.Text = Join-Path $dlg.SelectedPath 'mem0ry4ai' }
})
$tabInstall.Controls.Add($btnBrowse)

$chkSeparate          = New-Object System.Windows.Forms.CheckBox
$chkSeparate.Text     = "Keep memories outside the code folder (in $DataPath)"
$chkSeparate.Location = New-Object System.Drawing.Point(14, 100)
$chkSeparate.Size     = New-Object System.Drawing.Size(690, 22)
$chkSeparate.Checked  = $true
$tabInstall.Controls.Add($chkSeparate)

$lblSep           = New-Object System.Windows.Forms.Label
$lblSep.Text      = 'Recommended: updating the code then cannot touch your memories, and deleting the clone does not delete them.'
$lblSep.Location  = New-Object System.Drawing.Point(34, 120)
$lblSep.Size      = New-Object System.Drawing.Size(680, 18)
$lblSep.ForeColor = [System.Drawing.Color]::FromArgb(120, 120, 120)
$tabInstall.Controls.Add($lblSep)

$chkHooks          = New-Object System.Windows.Forms.CheckBox
$chkHooks.Text     = 'Register the Claude Code hooks (inject memory at session start, capture at end)'
$chkHooks.Location = New-Object System.Drawing.Point(14, 146)
$chkHooks.Size     = New-Object System.Drawing.Size(690, 22)
$chkHooks.Checked  = $true
$tabInstall.Controls.Add($chkHooks)

$chkStart          = New-Object System.Windows.Forms.CheckBox
$chkStart.Text     = 'Start the web UI when finished and open it in the browser'
$chkStart.Location = New-Object System.Drawing.Point(14, 170)
$chkStart.Size     = New-Object System.Drawing.Size(690, 22)
$chkStart.Checked  = $true
$tabInstall.Controls.Add($chkStart)

$btnInstall          = New-Object System.Windows.Forms.Button
$btnInstall.Text     = 'Install'
$btnInstall.Location = New-Object System.Drawing.Point(612, 196)
$btnInstall.Size     = New-Object System.Drawing.Size(90, 30)
$tabInstall.Controls.Add($btnInstall)

# --- uninstall tab
$tabRemove      = New-Object System.Windows.Forms.TabPage
$tabRemove.Text = '  Uninstall  '
$tabs.TabPages.Add($tabRemove)

$rSub          = New-Object System.Windows.Forms.Label
$rSub.Text     = 'Removes the Claude Code hooks, the scheduled maintenance job and the running web server.'
$rSub.Location = New-Object System.Drawing.Point(14, 12)
$rSub.Size     = New-Object System.Drawing.Size(690, 20)
$rSub.ForeColor = [System.Drawing.Color]::FromArgb(90, 90, 90)
$tabRemove.Controls.Add($rSub)

$rKeep           = New-Object System.Windows.Forms.Label
$rKeep.Text      = 'Your memories are KEPT. So is the code folder - delete that yourself when you are ready.'
$rKeep.Location  = New-Object System.Drawing.Point(14, 32)
$rKeep.Size      = New-Object System.Drawing.Size(690, 20)
$rKeep.ForeColor = [System.Drawing.Color]::FromArgb(90, 90, 90)
$tabRemove.Controls.Add($rKeep)

$rLblPath          = New-Object System.Windows.Forms.Label
$rLblPath.Text     = 'Installed in'
$rLblPath.Location = New-Object System.Drawing.Point(14, 64)
$rLblPath.Size     = New-Object System.Drawing.Size(200, 20)
$tabRemove.Controls.Add($rLblPath)

$rTxtPath          = New-Object System.Windows.Forms.TextBox
$rTxtPath.Text     = $InstallPath
$rTxtPath.Location = New-Object System.Drawing.Point(14, 84)
$rTxtPath.Size     = New-Object System.Drawing.Size(590, 24)
$tabRemove.Controls.Add($rTxtPath)

$rBrowse          = New-Object System.Windows.Forms.Button
$rBrowse.Text     = 'Browse'
$rBrowse.Location = New-Object System.Drawing.Point(612, 83)
$rBrowse.Size     = New-Object System.Drawing.Size(90, 26)
$rBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    if ($dlg.ShowDialog() -eq 'OK') { $rTxtPath.Text = $dlg.SelectedPath }
})
$tabRemove.Controls.Add($rBrowse)

$chkWipe           = New-Object System.Windows.Forms.CheckBox
$chkWipe.Text      = 'Also permanently delete all my memories'
$chkWipe.Location  = New-Object System.Drawing.Point(14, 120)
$chkWipe.Size      = New-Object System.Drawing.Size(690, 22)
$chkWipe.ForeColor = [System.Drawing.Color]::FromArgb(170, 30, 30)
$tabRemove.Controls.Add($chkWipe)

$lblWipe           = New-Object System.Windows.Forms.Label
$lblWipe.Text      = 'Off by default, and it stays off unless you mean it. This cannot be undone: the store is a git repo, but deleting the folder deletes the history with it.'
$lblWipe.Location  = New-Object System.Drawing.Point(34, 140)
$lblWipe.Size      = New-Object System.Drawing.Size(680, 32)
$lblWipe.ForeColor = [System.Drawing.Color]::FromArgb(150, 90, 90)
$tabRemove.Controls.Add($lblWipe)

$btnPreview          = New-Object System.Windows.Forms.Button
$btnPreview.Text     = 'Show what would be removed'
$btnPreview.Location = New-Object System.Drawing.Point(388, 196)
$btnPreview.Size     = New-Object System.Drawing.Size(200, 30)
$tabRemove.Controls.Add($btnPreview)

$btnRemove          = New-Object System.Windows.Forms.Button
$btnRemove.Text     = 'Uninstall'
$btnRemove.Location = New-Object System.Drawing.Point(600, 196)
$btnRemove.Size     = New-Object System.Drawing.Size(102, 30)
$tabRemove.Controls.Add($btnRemove)

# --- shared log
$lblLog           = New-Object System.Windows.Forms.Label
$lblLog.Text      = 'Every command is shown here before it runs.'
$lblLog.Location  = New-Object System.Drawing.Point(14, 270)
$lblLog.Size      = New-Object System.Drawing.Size(500, 18)
$lblLog.ForeColor = [System.Drawing.Color]::FromArgb(120, 120, 120)
$form.Controls.Add($lblLog)

$log            = New-Object System.Windows.Forms.TextBox
$log.Multiline  = $true
$log.ScrollBars = 'Vertical'
$log.ReadOnly   = $true
$log.WordWrap   = $false
$log.Location   = New-Object System.Drawing.Point(14, 290)
$log.Size       = New-Object System.Drawing.Size(730, 285)
$log.Font       = New-Object System.Drawing.Font('Consolas', 8.5)
$log.BackColor  = [System.Drawing.Color]::FromArgb(250, 250, 250)
$form.Controls.Add($log)
$script:LogBox = $log

$btnClose          = New-Object System.Windows.Forms.Button
$btnClose.Text     = 'Close'
$btnClose.Location = New-Object System.Drawing.Point(656, 585)
$btnClose.Size     = New-Object System.Drawing.Size(88, 30)
$btnClose.Add_Click({ $form.Close() })
$form.Controls.Add($btnClose)

$setBusy = {
    param([bool]$Busy)
    foreach ($c in @($btnInstall, $btnRemove, $btnPreview, $txtPath, $rTxtPath,
                     $btnBrowse, $rBrowse, $chkHooks, $chkStart, $chkSeparate, $chkWipe)) {
        $c.Enabled = -not $Busy
    }
}

$btnInstall.Add_Click({
    & $setBusy $true
    $log.Clear()
    try {
        $ok = Invoke-Install -Target $txtPath.Text.Trim() -Data $DataPath `
                             -DoHooks $chkHooks.Checked -DoStart $chkStart.Checked `
                             -SeparateData $chkSeparate.Checked
        if (-not $ok) {
            Add-Log ''
            Add-Log 'Install did not complete. Nothing was removed - fix what the log reports and try again.' 'error'
            & $setBusy $false
        } else { $btnClose.Text = 'Done' }
    } catch {
        Add-Log "unexpected error: $($_.Exception.Message)" 'error'
        & $setBusy $false
    }
})

$btnPreview.Add_Click({
    & $setBusy $true
    $log.Clear()
    try { $null = Invoke-Uninstall -Target $rTxtPath.Text.Trim() -DeleteMemories $chkWipe.Checked -Preview $true }
    catch { Add-Log "unexpected error: $($_.Exception.Message)" 'error' }
    & $setBusy $false
})

$btnRemove.Add_Click({
    $msg = "Remove the hooks, the scheduled job and the running server?`n`nYour memories will be kept."
    $icon = [System.Windows.Forms.MessageBoxIcon]::Question
    if ($chkWipe.Checked) {
        $msg = "This will PERMANENTLY DELETE every memory in $DataPath, along with its history.`n`nThere is no undo. Continue?"
        $icon = [System.Windows.Forms.MessageBoxIcon]::Warning
    }
    $answer = [System.Windows.Forms.MessageBox]::Show($msg, 'mem0ry4ai',
                  [System.Windows.Forms.MessageBoxButtons]::YesNo, $icon,
                  [System.Windows.Forms.MessageBoxDefaultButton]::Button2)
    if ($answer -ne 'Yes') { return }

    & $setBusy $true
    $log.Clear()
    try {
        # The wipe path needs a typed confirmation that uninstall.py reads from stdin, which this
        # window cannot provide. The dialog above is that confirmation, so the deletion is done
        # here, in the open, rather than by silently feeding the word through the pipe.
        $null = Invoke-Uninstall -Target $rTxtPath.Text.Trim() -DeleteMemories $false -Preview $false
        if ($chkWipe.Checked) {
            $store = $env:MEM_DATA_DIR
            if (-not $store) { $store = $DataPath }
            if (Test-Path $store) {
                Remove-Item -LiteralPath $store -Recurse -Force
                Add-Log "memories deleted: $store" 'warn'
            } else {
                Add-Log "no store found at $store - nothing deleted" 'info'
            }
        }
        Add-Log ''
        Add-Log 'Uninstall finished.' 'ok'
        $btnClose.Text = 'Done'
    } catch {
        Add-Log "unexpected error: $($_.Exception.Message)" 'error'
        & $setBusy $false
    }
})

Add-Log 'Ready. Pick a tab, review the paths, then press the button.'
Add-Log ''
$pre = Test-Prerequisite
Add-Log ("git    : " + $(if ($pre.Git)    { $pre.Git }    else { 'missing - will be installed' })) $(if ($pre.Git)    { 'ok' } else { 'warn' })
Add-Log ("python : " + $(if ($pre.Python) { $pre.Python } else { 'missing - will be installed' })) $(if ($pre.Python) { 'ok' } else { 'warn' })
if (-not $pre.Winget -and (-not $pre.Git -or -not $pre.Python)) {
    Add-Log 'winget is unavailable, so missing prerequisites cannot be installed for you.' 'warn'
    Add-Log 'Install Git and Python yourself, then run this again.' 'warn'
}

[void]$form.ShowDialog()
