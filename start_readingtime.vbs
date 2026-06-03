' 自动检测 VBS 所在目录，无需修改路径
Set objShell = CreateObject("Wscript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
scriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)

' 先切到项目目录，使用完整 Python 路径启动（避免 PATH 未加载问题）
objShell.CurrentDirectory = scriptDir
objShell.Run "cmd /c ""cd /d " & scriptDir & " && C:\Python314\python.exe -m readingtime.main start""", 0, False
