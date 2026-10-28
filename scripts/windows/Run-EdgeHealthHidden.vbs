Option Explicit

' Run through the GUI script host so no console is created on the operator desktop.
' //B on wscript suppresses script error dialogs as well.
If WScript.Arguments.Count <> 3 Then WScript.Quit 2
Dim shell, command, result
Set shell = CreateObject("WScript.Shell")
command = Quote(WScript.Arguments(0)) & " -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & Quote(WScript.Arguments(1)) & " -OutputDirectory " & Quote(WScript.Arguments(2))
' Wait for collection so Task Scheduler tracks duration, exit status and overlap.
result = shell.Run(command, 0, True)
WScript.Quit result

Function Quote(value)
    If InStr(value, Chr(34)) > 0 Then WScript.Quit 2
    ' Double trailing backslashes before the closing native-command quote.
    Dim suffix
    suffix = ""
    Do While Len(value) > 0 And Right(value, 1) = "\"
        suffix = suffix & "\\"
        value = Left(value, Len(value) - 1)
    Loop
    Quote = Chr(34) & value & suffix & Chr(34)
End Function
