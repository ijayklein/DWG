using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
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

        ed.WriteMessage("\n[LayerPdfExport] Start.\n");

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

        // FILEDIA 0 so -EXPORT takes the path on the command line (no dialog).
        // Editor.Command is void in AutoCAD.NET 25 — verify success via output file below.
        ed.Command("._FILEDIA", "0");

        // Full canvas: frame ALL model-space geometry in the active view once (all layers on).
        // ZOOM E in AcCoreConsole often does not match the true geometric extents; -EXPORT Display
        // then only showed part of the drawing. We set the view explicitly from unioned extents.
        ed.Command("._TILEMODE", "1");
        ed.Command("._UCS", "W");
        ZoomViewToFullModelExtents(ed, db);

        foreach (var keep in layerNames)
        {
            SetAllLayersOffState(db, keep, true);
            string safe = SanitizeFileName(keep);
            string pdfPath = Path.GetFullPath(Path.Combine(pdfDir, safe + ".pdf"));
            if (File.Exists(pdfPath))
                File.Delete(pdfPath);

            // SendStringToExecute queues until after this command returns — zip ran with 0 PDFs.
            // Editor.Command runs each -EXPORT to completion before we zip.
            // AutoCAD 2025 -EXPORT PDF: format → plot area → detailed [Y/N] → file name (FILEDIA 0).
            // Display = PDF uses the current view (set above to full model extents).
            ed.Command("._-EXPORT", "PDF", "Display", "No", pdfPath);
            if (!File.Exists(pdfPath))
                ed.WriteMessage($"\n[LayerPdfExport] EXPORT did not create file for layer {keep}: {pdfPath}");
            else
                ed.WriteMessage($"\n[LayerPdfExport] Exported layer {keep} -> {pdfPath}");
        }

        SetAllLayersOffState(db, null, false);

        int pdfCount = Directory.GetFiles(pdfDir, "*.pdf", SearchOption.TopDirectoryOnly).Length;
        if (layerNames.Count > 0 && pdfCount == 0)
            throw new InvalidOperationException(
                "No PDFs were produced; -EXPORT did not write files (check AcCore log for extra prompts).");

        string zipPath = Path.Combine(Directory.GetCurrentDirectory(), "layer_pdfs.zip");
        if (File.Exists(zipPath))
            File.Delete(zipPath);
        ZipFile.CreateFromDirectory(pdfDir, zipPath);
        ed.WriteMessage($"\n[LayerPdfExport] Wrote {zipPath} ({pdfCount} PDF file(s) in folder).");
    }

    /// <summary>
    /// Fit the editor view to the union of all model-space entity extents (WCS XY), with a small margin.
    /// Falls back to ZOOM E if no extents can be computed.
    /// </summary>
    private static void ZoomViewToFullModelExtents(Editor ed, Database db)
    {
        if (!TryGetModelSpaceExtents(db, out Extents3d ext))
        {
            ed.WriteMessage("\n[LayerPdfExport] No model extents; falling back to ZOOM EXTENTS.\n");
            ed.Command("._ZOOM", "E");
            return;
        }

        InflateExtents(ref ext, marginRatio: 0.02);

        double w = ext.MaxPoint.X - ext.MinPoint.X;
        double h = ext.MaxPoint.Y - ext.MinPoint.Y;
        if (w < 1e-9)
            w = 1.0;
        if (h < 1e-9)
            h = 1.0;

        double cx = (ext.MinPoint.X + ext.MaxPoint.X) * 0.5;
        double cy = (ext.MinPoint.Y + ext.MaxPoint.Y) * 0.5;

        var v = ed.GetCurrentView();
        v.CenterPoint = new Point2d(cx, cy);
        v.Width = w;
        v.Height = h;
        ed.SetCurrentView(v);
    }

    private static bool TryGetModelSpaceExtents(Database db, out Extents3d ext)
    {
        ext = default;
        bool any = false;

        using (var tr = db.TransactionManager.StartTransaction())
        {
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
            foreach (ObjectId id in ms)
            {
                if (tr.GetObject(id, OpenMode.ForRead) is not Entity ent)
                    continue;
                try
                {
                    var ge = ent.GeometricExtents;
                    if (!any)
                    {
                        ext = ge;
                        any = true;
                    }
                    else
                    {
                        ext.AddExtents(ge);
                    }
                }
                catch (Autodesk.AutoCAD.Runtime.Exception)
                {
                    // e.g. no valid extents for this entity
                }
            }

            tr.Commit();
        }

        if (any)
            return true;

        try
        {
            Point3d a = db.Extmin;
            Point3d b = db.Extmax;
            if (a.X < b.X && a.Y < b.Y)
            {
                ext = new Extents3d(a, b);
                return true;
            }
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
        }

        return false;
    }

    private static void InflateExtents(ref Extents3d ext, double marginRatio)
    {
        double dx = (ext.MaxPoint.X - ext.MinPoint.X) * marginRatio;
        double dy = (ext.MaxPoint.Y - ext.MinPoint.Y) * marginRatio;
        if (dx < 1e-12)
            dx = 1e-6;
        if (dy < 1e-12)
            dy = 1e-6;
        ext = new Extents3d(
            new Point3d(ext.MinPoint.X - dx, ext.MinPoint.Y - dy, ext.MinPoint.Z),
            new Point3d(ext.MaxPoint.X + dx, ext.MaxPoint.Y + dy, ext.MaxPoint.Z));
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
