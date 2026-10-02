Option Explicit
Dim shell, fso, base, exe
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base
exe = base & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(exe) Then exe = "pythonw.exe"
shell.Run Chr(34) & exe & Chr(34) & " " & Chr(34) & base & "\gui.py" & Chr(34), 1, False
