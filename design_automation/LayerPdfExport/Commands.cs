using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using System.Globalization;
using System.IO.Compression;
using System.Text.RegularExpressions;
using System.Threading;

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

        // Prefer the first paper layout (sheet + viewports) — that is usually the “full page” users
        // see in AutoCAD. Exporting Model alone often frames only a small model-space crop.
        var (usingPaper, layoutName) = SelectExportCanvas(ed, db);
        ed.Command("._ZOOM", "E");

        bool haveWindow = TryGetExtentsForCanvas(db, usingPaper, layoutName, out Extents3d fullExt);
        if (haveWindow)
        {
            InflateExtents(ref fullExt, marginRatio: 0.02);
            ed.WriteMessage(
                $"\n[LayerPdfExport] PDF plot window WCS min=({fullExt.MinPoint.X.ToString(CultureInfo.InvariantCulture)},{fullExt.MinPoint.Y.ToString(CultureInfo.InvariantCulture)}) max=({fullExt.MaxPoint.X.ToString(CultureInfo.InvariantCulture)},{fullExt.MaxPoint.Y.ToString(CultureInfo.InvariantCulture)})\n");
        }
        else
        {
            ed.WriteMessage("\n[LayerPdfExport] No extents for Window; using Display per PDF (model only).\n");
        }

        var win1 = haveWindow ? PointToCmd(fullExt.MinPoint) : "";
        var win2 = haveWindow ? PointToCmd(fullExt.MaxPoint) : "";

        foreach (var keep in layerNames)
        {
            SetAllLayersOffState(db, keep, true);
            string safe = SanitizeFileName(keep);
            string pdfPath = Path.GetFullPath(Path.Combine(pdfDir, safe + ".pdf"));
            if (File.Exists(pdfPath))
                File.Delete(pdfPath);

            // Model: Window or Display. Paper layout: Current layout (not Extents/Window — different prompts).
            if (usingPaper)
                CommandExportPdf(ed, true, pdfPath);
            else if (haveWindow)
                ed.Command("._-EXPORT", "PDF", "Window", win1, win2, "No", pdfPath);
            else
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
    /// Single PDF with all layers on. Activates the first paper layout when the DWG has one,
    /// otherwise Model; then <c>ZOOM</c> <c>E</c> and <c>-EXPORT</c> using layout vs model prompts.
    /// </summary>
    [CommandMethod("ExportFlatPdf", CommandFlags.Modal)]
    public static void ExportFlatPdf()
    {
        var doc = Application.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("No active document.");
        var db = doc.Database;
        var ed = doc.Editor;

        ed.WriteMessage("\n[LayerPdfExport] ExportFlatPdf — all layers visible, one PDF.\n");

        SetAllLayersOffState(db, null, false);

        string pdfDir = Path.Combine(Directory.GetCurrentDirectory(), "_layerpdf_out");
        if (Directory.Exists(pdfDir))
            Directory.Delete(pdfDir, true);
        Directory.CreateDirectory(pdfDir);

        ed.Command("._FILEDIA", "0");
        var (usingPaper, _) = SelectExportCanvas(ed, db);
        ed.Command("._ZOOM", "E");

        string pdfPath = Path.GetFullPath(Path.Combine(pdfDir, "flat.pdf"));
        if (File.Exists(pdfPath))
            File.Delete(pdfPath);

        // Model: PDF → Extents → No → path. Paper layout: PDF → Current layout | All layouts → No → path
        // (see AcCore log — “Extents” is invalid on a layout tab).
        CommandExportPdf(ed, usingPaper, pdfPath);

        if (!File.Exists(pdfPath))
            throw new InvalidOperationException("Flat PDF was not produced; check AcCore log for -EXPORT prompts.");

        string zipPath = Path.Combine(Directory.GetCurrentDirectory(), "layer_pdfs.zip");
        if (File.Exists(zipPath))
            File.Delete(zipPath);
        ZipFile.CreateFromDirectory(pdfDir, zipPath);
        ed.WriteMessage($"\n[LayerPdfExport] Wrote {zipPath} (contains flat.pdf, all layers).\n");
    }

    /// <summary>
    /// One PDF per paper layout tab (all layers on each), named from the layout.
    /// Model-only drawings get a single <c>model.pdf</c>. Zipped to <c>layer_pdfs.zip</c>.
    /// </summary>
    [CommandMethod("ExportAllLayoutPdfs", CommandFlags.Modal)]
    public static void ExportAllLayoutPdfs()
    {
        var doc = Application.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("No active document.");
        var db = doc.Database;
        var ed = doc.Editor;

        ed.WriteMessage("\n[LayerPdfExport] ExportAllLayoutPdfs — one PDF per layout tab (all layers).\n");

        SetAllLayersOffState(db, null, false);

        string pdfDir = Path.Combine(Directory.GetCurrentDirectory(), "_layerpdf_out");
        if (Directory.Exists(pdfDir))
            Directory.Delete(pdfDir, true);
        Directory.CreateDirectory(pdfDir);

        ed.Command("._FILEDIA", "0");

        var layouts = GetPaperLayoutNamesOrdered(db);
        if (layouts.Count == 0)
        {
            ed.WriteMessage("\n[LayerPdfExport] No paper layouts — exporting Model to model.pdf.\n");
            ed.Command("._TILEMODE", "1");
            ed.Command("._UCS", "W");
            ed.Command("._ZOOM", "E");
            string modelPath = Path.GetFullPath(Path.Combine(pdfDir, "model.pdf"));
            CommandExportPdf(ed, false, modelPath);
            if (!File.Exists(modelPath))
                throw new InvalidOperationException("model.pdf was not produced.");
        }
        else
        {
            ed.WriteMessage($"\n[LayerPdfExport] {layouts.Count} paper layout(s) to export.\n");
            foreach (string layoutName in layouts)
            {
                ActivatePaperLayout(db, layoutName);
                ed.Command("._ZOOM", "E");
                string safe = SanitizeFileName(layoutName);
                string pdfPath = Path.GetFullPath(Path.Combine(pdfDir, $"{safe}.pdf"));
                if (File.Exists(pdfPath))
                    File.Delete(pdfPath);
                CommandExportPdf(ed, true, pdfPath);
                if (!File.Exists(pdfPath))
                    ed.WriteMessage($"\n[LayerPdfExport] Warning: no PDF for layout \"{layoutName}\" -> {pdfPath}");
                else
                    ed.WriteMessage($"\n[LayerPdfExport] Layout \"{layoutName}\" -> {pdfPath}");
            }
        }

        int pdfCount = Directory.GetFiles(pdfDir, "*.pdf", SearchOption.TopDirectoryOnly).Length;
        if (pdfCount == 0)
            throw new InvalidOperationException("No PDFs were produced.");

        string zipPath = Path.Combine(Directory.GetCurrentDirectory(), "layer_pdfs.zip");
        if (File.Exists(zipPath))
            File.Delete(zipPath);
        ZipFile.CreateFromDirectory(pdfDir, zipPath);
        ed.WriteMessage($"\n[LayerPdfExport] Wrote {zipPath} ({pdfCount} PDF file(s)).\n");
    }

    /// <summary>
    /// One DWG per paper layout. Matches <see cref="ExportAllLayoutPdfs"/> layout activation
    /// (<see cref="ActivatePaperLayout"/> + <c>ZOOM E</c>), but AutoCAD has no <c>-EXPORT</c> → DWG for “current
    /// layout”; this command uses <c>-EXPORTLAYOUT</c> to create a new drawing from the active
    /// layout (sheet content to model space per AutoCAD). Model-only drawings copy the host as
    /// <c>model.dwg</c>. Zipped to <c>layout_dwgs.zip</c>.
    /// </summary>
    [CommandMethod("ExportAllLayoutDwgs", CommandFlags.Modal)]
    public static void ExportAllLayoutDwgs()
    {
        var doc = Application.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("No active document.");
        var db = doc.Database;
        var ed = doc.Editor;

        ed.WriteMessage("\n[LayerPdfExport] ExportAllLayoutDwgs — one DWG per layout tab.\n");

        // Same preamble as ExportAllLayoutPdfs (all layers on, FILEDIA off).
        SetAllLayersOffState(db, null, false);

        string dwgDir = Path.Combine(Directory.GetCurrentDirectory(), "_layoutdwg_out");
        if (Directory.Exists(dwgDir))
            Directory.Delete(dwgDir, true);
        Directory.CreateDirectory(dwgDir);

        ed.Command("._FILEDIA", "0");

        var layouts = GetPaperLayoutNamesOrdered(db);
        if (layouts.Count == 0)
        {
            ed.WriteMessage("\n[LayerPdfExport] No paper layouts — writing model.dwg (copy of input).\n");
            string modelPath = Path.GetFullPath(Path.Combine(dwgDir, "model.dwg"));
            if (File.Exists(modelPath))
                File.Delete(modelPath);
            string src = db.Filename;
            if (string.IsNullOrWhiteSpace(src) || !File.Exists(src))
                src = doc.Name;
            if (string.IsNullOrWhiteSpace(src) || !File.Exists(src))
                throw new InvalidOperationException("Cannot resolve source DWG path for model copy.");
            File.Copy(src, modelPath, overwrite: true);
            ed.WriteMessage($"\n[LayerPdfExport] model.dwg <- {src}\n");
        }
        else
        {
            ed.WriteMessage($"\n[LayerPdfExport] {layouts.Count} paper layout(s) to export as DWG.\n");
            foreach (string layoutName in layouts)
            {
                ActivatePaperLayout(db, layoutName);
                ed.Command("._-ZOOM", "Extents");

                string safe = SanitizeFileName(layoutName);
                string dwgPath = Path.GetFullPath(Path.Combine(dwgDir, $"{safe}.dwg"));
                if (File.Exists(dwgPath))
                    File.Delete(dwgPath);
                ed.Command("._-EXPORTLAYOUT", dwgPath);
                if (!File.Exists(dwgPath))
                    ed.WriteMessage($"\n[LayerPdfExport] Warning: no DWG for layout \"{layoutName}\" -> {dwgPath}");
                else
                    ed.WriteMessage($"\n[LayerPdfExport] Layout \"{layoutName}\" -> {dwgPath}");
            }
        }

        int dwgCount = Directory.GetFiles(dwgDir, "*.dwg", SearchOption.TopDirectoryOnly).Length;
        if (dwgCount == 0)
            throw new InvalidOperationException("No DWG files were produced.");

        string zipPath = Path.Combine(Directory.GetCurrentDirectory(), "layout_dwgs.zip");
        if (File.Exists(zipPath))
            File.Delete(zipPath);
        ZipFile.CreateFromDirectory(dwgDir, zipPath);
        ed.WriteMessage($"\n[LayerPdfExport] Wrote {zipPath} ({dwgCount} DWG file(s)).\n");
    }

    /// <summary>
    /// All non-Model layouts in <b>layout dictionary order</b> (usually the same as layout tab
    /// order). Do not sort alphabetically: that can reorder tabs and activate a heavy layout first,
    /// which has crashed AcCoreConsole on some DWGs during regen.
    /// </summary>
    private static List<string> GetPaperLayoutNamesOrdered(Database db)
    {
        var list = new List<string>();
        using var tr = db.TransactionManager.StartTransaction();
        var dict = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
        foreach (DBDictionaryEntry e in dict)
        {
            if (!string.Equals(e.Key, "Model", StringComparison.OrdinalIgnoreCase))
                list.Add(e.Key);
        }

        tr.Commit();
        return list;
    }

    /// <summary>
    /// <c>-EXPORT</c> PDF prompts differ: Model uses Display / Extents / Window; an active layout
    /// uses Current layout / All layouts (see DA report if keywords change).
    /// </summary>
    private static void CommandExportPdf(Editor ed, bool paperLayout, string pdfPath)
    {
        if (paperLayout)
            ed.Command("._-EXPORT", "PDF", "Current", "No", pdfPath);
        else
            ed.Command("._-EXPORT", "PDF", "Extents", "No", pdfPath);
    }

    /// <summary>WCS point as command-line "x,y" (invariant), for -EXPORT Window corners.</summary>
    private static string PointToCmd(Point3d p) =>
        $"{p.X.ToString(CultureInfo.InvariantCulture)},{p.Y.ToString(CultureInfo.InvariantCulture)}";

    /// <summary>
    /// Switch to the drawing’s “page”: first paper layout if any, otherwise Model + WORLD UCS.
    /// </summary>
    private static (bool UsingPaperLayout, string? LayoutName) SelectExportCanvas(Editor ed, Database db)
    {
        string? paper = GetFirstPaperLayoutName(db);
        if (string.IsNullOrEmpty(paper))
        {
            ed.Command("._TILEMODE", "1");
            ed.Command("._UCS", "W");
            ed.WriteMessage("\n[LayerPdfExport] Canvas: Model (no paper layouts).\n");
            return (false, null);
        }

        ActivatePaperLayout(db, paper);
        ed.WriteMessage($"\n[LayerPdfExport] Canvas: paper layout \"{paper}\".\n");
        return (true, paper);
    }

    /// <summary>
    /// Paper layout switch via API + <see cref="Database.TileMode"/>false; avoids AcCore crashes
    /// during viewport regen after <c>LAYOUT S</c> on some DWGs (command-line switch is less stable in DA).
    /// </summary>
    private static void ActivatePaperLayout(Database db, string layoutName, int waitMs = 150)
    {
        db.TileMode = false;
        LayoutManager.Current.CurrentLayout = layoutName;
        if (waitMs > 0)
            Thread.Sleep(waitMs);
    }

    /// <summary>First non-Model layout; prefers Layout1 when present (common default tab).</summary>
    private static string? GetFirstPaperLayoutName(Database db)
    {
        using var tr = db.TransactionManager.StartTransaction();
        var dict = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
        string? first = null;
        foreach (DBDictionaryEntry e in dict)
        {
            if (string.Equals(e.Key, "Model", StringComparison.OrdinalIgnoreCase))
                continue;
            if (string.Equals(e.Key, "Layout1", StringComparison.OrdinalIgnoreCase))
            {
                tr.Commit();
                return e.Key;
            }

            first ??= e.Key;
        }

        tr.Commit();
        return first;
    }

    /// <summary>Extents for -EXPORT Window: paperspace block if in a layout, else model space.</summary>
    private static bool TryGetExtentsForCanvas(Database db, bool paperLayout, string? layoutName, out Extents3d ext)
    {
        if (paperLayout && !string.IsNullOrEmpty(layoutName) && TryGetPaperLayoutExtents(db, layoutName, out ext))
            return true;
        return TryGetModelSpaceExtents(db, out ext);
    }

    private static bool TryGetPaperLayoutExtents(Database db, string layoutName, out Extents3d ext)
    {
        ext = default;
        bool any = false;
        using var tr = db.TransactionManager.StartTransaction();
        var dict = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
        ObjectId layId;
        try
        {
            layId = dict.GetAt(layoutName);
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            tr.Commit();
            return false;
        }

        var layout = (Layout)tr.GetObject(layId, OpenMode.ForRead);
        var btr = (BlockTableRecord)tr.GetObject(layout.BlockTableRecordId, OpenMode.ForRead);
        foreach (ObjectId id in btr)
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
            }
        }

        tr.Commit();
        return any;
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
