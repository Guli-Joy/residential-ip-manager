Option Explicit

Dim appCommand, exitCode, fileSystem, item, needSetup, probeCommand
Dim projectRoot, pythonExe, pythonwExe, setupCommand, shell

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
projectRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
pythonExe = projectRoot & "\.venv\Scripts\python.exe"
pythonwExe = projectRoot & "\.venv\Scripts\pythonw.exe"
needSetup = Not fileSystem.FileExists(pythonwExe)

If Not needSetup Then
    probeCommand = Quote(pythonExe) _
        & " -c " & Quote("import httpx, PySide6, yaml, residential_ip_manager")
    exitCode = shell.Run(probeCommand, 0, True)
    needSetup = (exitCode <> 0)
End If

If needSetup Then
    setupCommand = Quote(shell.ExpandEnvironmentStrings("%SystemRoot%") _
        & "\System32\WindowsPowerShell\v1.0\powershell.exe") _
        & " -NoProfile -ExecutionPolicy Bypass -File " _
        & Quote(projectRoot & "\scripts\setup.ps1")
    exitCode = shell.Run(setupCommand, 0, True)
    If exitCode <> 0 Then
        MsgBox "Startup setup failed.", vbCritical, "Residential IP Manager"
        WScript.Quit exitCode
    End If
End If

appCommand = Quote(pythonwExe) & " -m residential_ip_manager.main"
For Each item In WScript.Arguments
    appCommand = appCommand & " " & Quote(CStr(item))
Next

shell.CurrentDirectory = projectRoot
shell.Run appCommand, 1, False

Function Quote(ByVal value)
    Quote = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
