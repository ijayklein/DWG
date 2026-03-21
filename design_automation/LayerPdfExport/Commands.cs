using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Runtime;
using System.IO.Compression;
using System.Text.RegularExpressions;

[assembly: CommandClass(typeof(LayerPdfExport.Commands))]
[assembly: ExtensionApplication(typeof(LayerPdfExport.PluginApp))]

namespace LayerPdfExport
{
/// <summary>Registers the managed module with accoreconsole (ExtensionApplication(null) skips init).</summary>
public class PluginApp : IExtensionApplication
{
    public void Initialize() { }

    public void Terminate() { }
}

/// <summary>
/// Design Automation entry: run command <c>ExportLayerPdfs</c> after the host DWG is opened.
/// Isolates each layer (others off), runs EXPORT to PDF, zips all PDFs to <c>layer_pdfs.zip</c> in the working folder.
/// </summary>
public class Commands
{
    [CommandMethod("ExportLayerPdfs", CommandFlags.Modal)]
    public static void ExportLayerPdfs()
    {
        var doc = Application.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("No active document.");
        var db = doc.Database;
        var ed = doc.Editor;

        ed.WriteMessage("\n[LayerPdfExport] Start.");

        var layerNames = new List<string>();
        using (var tr = db.TransactionManager.StartTransaction())
        {
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            foreach (ObjectId id in lt)
            {
                var ltr = (LayerTableRecord)tr.GetObject(id, OpenMode.ForRead);
                if (ltr.IsDependent)
                    continue;
                layerNames.Add(ltr.Name);
            }
            tr.Commit();
        }

        string pdfDir = Path.Combine(Directory.GetCurrentDirectory(), "_layerpdf_out");
        if (Directory.Exists(pdfDir))
            Directory.Delete(pdfDir, true);
        Directory.CreateDirectory(pdfDir);

        // Ensure all layers on first (baseline).
        SetAllLayersOffState(db, null, false);

        foreach (var keep in layerNames)
        {
            SetAllLayersOffState(db, keep, true);
            string safe = SanitizeFileName(keep);
            string pdfPath = Path.GetFullPath(Path.Combine(pdfDir, safe + ".pdf"));
            if (File.Exists(pdfPath))
                File.Delete(pdfPath);

            doc.SendStringToExecute("._FILEDIA\n0\n", true, false, false);
            // Do not Thread.Sleep or Regen() here — blocks the AcCore message loop and trips DA heartbeat.
            doc.SendStringToExecute(
                $"._-EXPORT\nPDF\n{pdfPath}\n\n\n\n",
                true,
                false,
                false);
            ed.WriteMessage($"\n[LayerPdfExport] Queued EXPORT for layer {keep} -> {pdfPath}");
        }

        SetAllLayersOffState(db, null, false);

        string zipPath = Path.Combine(Directory.GetCurrentDirectory(), "layer_pdfs.zip");
        if (File.Exists(zipPath))
            File.Delete(zipPath);
        ZipFile.CreateFromDirectory(pdfDir, zipPath);
        var written = Directory.GetFiles(pdfDir, "*.pdf", SearchOption.TopDirectoryOnly);
        ed.WriteMessage($"\n[LayerPdfExport] Wrote {zipPath} ({written.Length} PDF file(s) in folder).");
    }

    /// <summary>If <paramref name="onlyOn"/> is null, set all layers to <paramref name="off"/>; else only that layer on.</summary>
    private static void SetAllLayersOffState(Database db, string? onlyOn, bool othersOff)
    {
        using var tr = db.TransactionManager.StartTransaction();
        var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
        foreach (ObjectId id in lt)
        {
            var ltr = (LayerTableRecord)tr.GetObject(id, OpenMode.ForWrite);
            if (onlyOn == null)
            {
                ltr.IsOff = false;
            }
            else
            {
                bool isKeep = string.Equals(ltr.Name, onlyOn, StringComparison.OrdinalIgnoreCase);
                ltr.IsOff = othersOff && !isKeep;
            }
        }
        tr.Commit();
    }

    private static string SanitizeFileName(string name) =>
        Regex.Replace(name, @"[^\w\.\-]", "_", RegexOptions.None, TimeSpan.FromSeconds(1));
}
}
