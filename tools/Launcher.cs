using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class Launcher
{
    private static string Quote(string value)
    {
        var text = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { text.Append('\\', slashes * 2 + 1); text.Append(c); slashes = 0; continue; }
            text.Append('\\', slashes); slashes = 0; text.Append(c);
        }
        text.Append('\\', slashes * 2); text.Append('"');
        return text.ToString();
    }

    [STAThread]
    private static void Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        try
        {
            var command = new StringBuilder(Quote(Path.Combine(root, "app.py")));
            foreach (string arg in args) command.Append(" ").Append(Quote(arg));
            var start = new ProcessStartInfo(Path.Combine(root, "runtime", "pythonw.exe"), command.ToString());
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            var process = Process.Start(start);
            process.WaitForExit();
            if (process.ExitCode != 0) MessageBox.Show("启动或运行失败，请查看程序目录的 startup-error.log。", "发票工作台");
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "发票工作台启动失败");
        }
    }
}
