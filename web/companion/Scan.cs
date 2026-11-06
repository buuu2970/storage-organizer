using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Text;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using System.Web.Script.Serialization;

class Scan {
    static readonly HashSet<string> Videos = new HashSet<string>(
        ".mp4 .mkv .avi .mov .wmv .flv .webm .m4v .mpg .mpeg .ts .m2ts .mts .m2t .vob .3gp".Split(),StringComparer.OrdinalIgnoreCase);
    static readonly HashSet<string> Excluded = new HashSet<string>(
        new[]{"Windows","Windows.old","Program Files","Program Files (x86)","ProgramData","AppData","$Recycle.Bin","System Volume Information","Recovery",".git","node_modules"},StringComparer.OrdinalIgnoreCase);
    static readonly List<Video> movies=new List<Video>();
    static int skipped;
    public class Video {
        public string name, path;
        public long size;
        [ScriptIgnore] public string full;
        [ScriptIgnore] public DateTime modified;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct FileInfoNative {
        public uint attr;
        public System.Runtime.InteropServices.ComTypes.FILETIME created,accessed,written;
        public uint volume,highSize,lowSize,links,highIndex,lowIndex;
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle handle,out FileInfoNative data);
    static bool Unsupported(FileAttributes a) {
        // Reparse points, offline files and cloud recall placeholders.
        return ((int)a & (0x400|0x1000|0x40000|0x400000|0x4))!=0;
    }
    static bool IsTransportStream(string path) {
        using(var f=File.OpenRead(path)){
            byte[] b=new byte[620];int n=f.Read(b,0,b.Length);
            foreach(int stride in new[]{188,192,204})
                for(int start=0;start<stride && start+2*stride<n;start++)
                    if(b[start]==71 && b[start+stride]==71 && b[start+2*stride]==71)return true;
            return false;
        }
    }
    static void Walk(string root) {
        var dirs=new Stack<string>();dirs.Push(root);
        while(dirs.Count>0){
            string[] entries;
            try{entries=Directory.GetFileSystemEntries(dirs.Pop());}
            catch(IOException){skipped++;continue;}catch(UnauthorizedAccessException){skipped++;continue;}
            foreach(string path in entries){
                try {
                    var a=File.GetAttributes(path);
                    if(Unsupported(a)){skipped++;continue;}
                    if((a&FileAttributes.Directory)!=0){
                        if(!Excluded.Contains(Path.GetFileName(path)))dirs.Push(path);
                        continue;
                    }
                    string ext=Path.GetExtension(path);
                    if(!Videos.Contains(ext) || (ext.Equals(".ts",StringComparison.OrdinalIgnoreCase)&&!IsTransportStream(path)))continue;
                    var f=new FileInfo(path);
                    movies.Add(new Video{name=f.Name,path=f.DirectoryName,full=path,size=f.Length,modified=f.LastWriteTimeUtc});
                    if(movies.Count%100==0)Console.WriteLine("録画 {0} 本を発見",movies.Count);
                }catch(IOException){skipped++;}catch(UnauthorizedAccessException){skipped++;}
            }
        }
    }
    static List<int[]> Match(){
        var groups=new List<int[]>();var seen=new HashSet<string>();
        foreach(var bucket in Enumerable.Range(0,movies.Count).Where(i=>movies[i].size>0).GroupBy(i=>movies[i].size).Where(g=>g.Count()>1)){
            var hashes=new Dictionary<string,List<int>>();
            foreach(int i in bucket){
                var v=movies[i];
                try {
                    using(var f=File.Open(v.full,FileMode.Open,FileAccess.Read,FileShare.Read))
                    using(var sha=SHA256.Create()){
                        FileInfoNative id;
                        if(!GetFileInformationByHandle(f.SafeFileHandle,out id) || Unsupported((FileAttributes)id.attr)
                          || f.Length!=v.size || File.GetLastWriteTimeUtc(v.full)!=v.modified){skipped++;continue;}
                        string key=id.volume+":"+id.highIndex+":"+id.lowIndex;
                        if((id.highIndex!=0||id.lowIndex!=0)&&!seen.Add(key))continue;
                        string h=Convert.ToBase64String(sha.ComputeHash(f));
                        if(!hashes.ContainsKey(h))hashes[h]=new List<int>();
                        hashes[h].Add(i);
                    }
                }catch(IOException){skipped++;}catch(UnauthorizedAccessException){skipped++;}
            }
            groups.AddRange(hashes.Values.Where(g=>g.Count>1).Select(g=>g.OrderBy(i=>movies[i].modified).ThenBy(i=>movies[i].full,StringComparer.OrdinalIgnoreCase).ToArray()));
        }
        return groups;
    }
    static int Main(string[] args){
        try{
            var roots=args.Length==2 ? new[]{Path.GetFullPath(args[0])} : DriveInfo.GetDrives().Where(d=>d.DriveType==DriveType.Fixed && d.IsReady).Select(d=>d.RootDirectory.FullName).ToArray();
            if(args.Length!=2){
                Console.WriteLine("調査対象: "+String.Join(" / ",roots));
                Console.WriteLine("動画は変更しません。Enter で開始、Ctrl+C で中止します。");Console.ReadLine();
            }
            foreach(string root in roots){Console.WriteLine("録画を探しています: "+root);Walk(root);}
            Console.WriteLine("同じサイズの録画の内容を照合しています…");
            var matches=Match();
            var json=new JavaScriptSerializer{MaxJsonLength=Int32.MaxValue}.Serialize(new{rows=movies,groups=matches,roots=roots,skipped=skipped});
            var dir=args.Length==2?args[1]:Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"RecSort","Reports");
            Directory.CreateDirectory(dir);
            string dest=Path.Combine(dir,"RecSort-"+Guid.NewGuid().ToString("N")+".html");
            using(var s=typeof(Scan).Assembly.GetManifestResourceStream("RecSort.html"))
            using(var reader=new StreamReader(s)){
                string html=reader.ReadToEnd().Replace("<!--SCAN_DATA-->","<script type=\"application/json\" id=\"scanData\">"+json.Replace("<","\\u003c")+"</script>");
                File.WriteAllText(dest,html,new UTF8Encoding(false));
            }
            Console.WriteLine("調査完了: {0} 本、完全重複 {1} グループ。\n{2}",movies.Count,matches.Count,dest);
            if(args.Length!=2)Process.Start(new ProcessStartInfo(dest){UseShellExecute=true});
            return 0;
        }catch(Exception e){Console.Error.WriteLine(e.Message);if(args.Length!=2)Console.ReadLine();return 1;}
    }
}
