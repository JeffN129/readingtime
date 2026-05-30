' 自动检测 VBS 所在目录，无需修改路径
Set objShell = CreateObject("Wscript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
scriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\start_readingtime.bat"

' 先切到项目目录再运行
objShell.CurrentDirectory = scriptDir
objShell.Run "cmd /c ""cd /d " & scriptDir & " && readingtime start""", 0, False
