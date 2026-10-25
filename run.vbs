Set fs = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
python = folder & "\.venv\Scripts\pythonw.exe"
If Not fs.FileExists(python) Then
    MsgBox "Run setup.bat first to install the local dependencies.", 48, "Doorbell Listener"
    WScript.Quit 1
End If
sh.CurrentDirectory = folder
sh.Run Chr(34) & python & Chr(34) & " " & Chr(34) & folder & "\widget.py" & Chr(34), 0, False
