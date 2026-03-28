using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using System.Globalization;
using System.IO.Compression;
using System.Text.Json;
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

        SetAllLayersOffState(db, null, false);

        ed.Command("._FILEDIA", "0");

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
    /// One DWG per paper layout — erases non-target layout entities from the active database,
    /// <c>SaveAs</c> to produce a file with Model + one layout (with viewports), then restores.
    /// Each output preserves full model space, layers, blocks, and the target layout tab with
    /// functioning viewports. Zipped to <c>layout_dwgs.zip</c>.
    /// <para>This is the <b>full-fidelity</b> option. For a lightweight paperspace-only extract,
    /// see <see cref="ExportAllLayoutDwgsWblock"/>.</para>
    /// </summary>
    [CommandMethod("ExportAllLayoutDwgs", CommandFlags.Modal)]
    public static void ExportAllLayoutDwgs()
    {
        var doc = Application.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("No active document.");
        var db = doc.Database;
        var ed = doc.Editor;

        ed.WriteMessage("\n[LayerPdfExport] ExportAllLayoutDwgs — one DWG per layout tab.\n");

        MakeAllLayersVisible(db);

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

            var layoutEntityMap = BuildLayoutEntityMap(db, layouts);

            var allIds = layoutEntityMap.Values.SelectMany(x => x).ToList();
            EraseObjectIds(db, allIds);

            foreach (string layoutName in layouts)
            {
                if (layoutEntityMap.TryGetValue(layoutName, out var targetIds) && targetIds.Count > 0)
                    UnEraseObjectIds(db, targetIds);

                // Clip model space to entities within this layout's viewport(s).
                var vpExtents = GetViewportModelSpaceExtents(db, layoutName);
                List<ObjectId> modelErased = new();
                if (vpExtents.Count > 0)
                {
                    modelErased = CollectModelSpaceEntitiesToErase(db, vpExtents);
                    EraseObjectIds(db, modelErased);
                    ed.WriteMessage($"\n[LayerPdfExport] Layout \"{layoutName}\": erased {modelErased.Count} model space entities outside viewport(s).\n");
                }

                ActivatePaperLayout(db, layoutName);
                ed.Command("._ZOOM", "E");

                string safe = SanitizeFileName(layoutName);
                string dwgPath = Path.GetFullPath(Path.Combine(dwgDir, $"{safe}.dwg"));
                if (File.Exists(dwgPath))
                    File.Delete(dwgPath);
                ed.Command("._-WBLOCK", dwgPath, "*");

                // Restore model space entities for next iteration.
                if (modelErased.Count > 0)
                    UnEraseObjectIds(db, modelErased);

                if (targetIds != null && targetIds.Count > 0)
                    EraseObjectIds(db, targetIds);

                if (File.Exists(dwgPath))
                    ed.WriteMessage($"\n[LayerPdfExport] Layout \"{layoutName}\" -> {dwgPath}");
                else
                    ed.WriteMessage($"\n[LayerPdfExport] Warning: no DWG for layout \"{layoutName}\"");
            }

            UnEraseObjectIds(db, allIds);
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
    /// Write <c>layout_names.json</c> to CWD — a JSON array of paper layout tab names (dictionary
    /// order). Used by the fan-out pipeline to discover layouts before spawning per-layout WorkItems.
    /// </summary>
    [CommandMethod("ListLayoutNames", CommandFlags.Modal)]
    public static void ListLayoutNames()
    {
        var doc = Application.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("No active document.");
        var db = doc.Database;
        var ed = doc.Editor;

        var layouts = GetPaperLayoutNamesOrdered(db);
        string jsonPath = Path.Combine(Directory.GetCurrentDirectory(), "layout_names.json");
        File.WriteAllText(jsonPath, JsonSerializer.Serialize(layouts));
        ed.WriteMessage($"\n[LayerPdfExport] Wrote {jsonPath} ({layouts.Count} layout name(s)).\n");
    }

    /// <summary>
    /// Export exactly one layout to DWG. Reads the layout name from <c>layout_name.txt</c>.
    /// Erases all non-target layout entities from the active database, then <c>SaveAs</c>.
    /// No restore needed since each fan-out WorkItem opens a fresh copy of the input DWG.
    /// Output preserves model space + one layout tab with functioning viewports.
    /// </summary>
    [CommandMethod("ExportSingleLayoutDwg", CommandFlags.Modal)]
    public static void ExportSingleLayoutDwg()
    {
        var doc = Application.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("No active document.");
        var db = doc.Database;
        var ed = doc.Editor;

        string paramPath = Path.Combine(Directory.GetCurrentDirectory(), "layout_name.txt");
        if (!File.Exists(paramPath))
            throw new FileNotFoundException("layout_name.txt not found in CWD — pass it as a WorkItem argument.");
        string layoutName = File.ReadAllText(paramPath).Trim();
        if (string.IsNullOrEmpty(layoutName))
            throw new InvalidOperationException("layout_name.txt is empty.");

        ed.WriteMessage($"\n[LayerPdfExport] ExportSingleLayoutDwg — layout \"{layoutName}\".\n");

        MakeAllLayersVisible(db);

        var allLayouts = GetPaperLayoutNamesOrdered(db);
        if (!allLayouts.Any(n => string.Equals(n, layoutName, StringComparison.OrdinalIgnoreCase)))
            throw new InvalidOperationException($"Layout \"{layoutName}\" not found in DWG (have: {string.Join(", ", allLayouts)}).");

        string dwgDir = Path.Combine(Directory.GetCurrentDirectory(), "_layoutdwg_out");
        if (Directory.Exists(dwgDir))
            Directory.Delete(dwgDir, true);
        Directory.CreateDirectory(dwgDir);

        ed.Command("._FILEDIA", "0");

        // Clip model space (viewport extents must be read before any layouts are deleted).
        var vpExtents = GetViewportModelSpaceExtents(db, layoutName);
        if (vpExtents.Count > 0)
        {
            var modelToErase = CollectModelSpaceEntitiesToErase(db, vpExtents);
            EraseObjectIds(db, modelToErase);
            ed.WriteMessage($"\n[LayerPdfExport] Erased {modelToErase.Count} model space entities outside viewport(s).\n");
        }
        else
        {
            ed.WriteMessage("\n[LayerPdfExport] No viewports found — model space kept intact.\n");
        }

        // Erase all entities inside non-target layouts (empties them before deletion).
        var nonTargetLayouts = allLayouts
            .Where(n => !string.Equals(n, layoutName, StringComparison.OrdinalIgnoreCase))
            .ToList();
        var layoutEntityMap = BuildLayoutEntityMap(db, nonTargetLayouts);
        EraseObjectIds(db, layoutEntityMap.Values.SelectMany(x => x).ToList());

        // Activate the target layout BEFORE deleting others.
        // LayoutManager.DeleteLayout cannot delete the *current* layout; activating the target
        // layout first ensures every non-target layout is deletable.
        ActivatePaperLayout(db, layoutName);
        ed.Command("._ZOOM", "E");

        // Clear per-viewport layer freezes — the source DWG VP-froze many layers in each
        // viewport to hide other floor plans' content; those states survive into the output and
        // make layers appear inaccessible (hover-highlight and LAYOFF don't respond).
        // The model-space clip already removed the unwanted geometry, so VP-freezes serve no
        // purpose and only break the layer workflow.
        ClearViewportLayerFreezes(ed);

        // Delete non-target layouts so they do not appear as (empty) tabs in the output DWG.
        int deleted = 0;
        foreach (string nonTarget in nonTargetLayouts)
        {
            try
            {
                LayoutManager.Current.DeleteLayout(nonTarget);
                deleted++;
            }
            catch (System.Exception ex)
            {
                ed.WriteMessage($"\n[LayerPdfExport] Warning: could not delete layout \"{nonTarget}\": {ex.Message}\n");
            }
        }
        ed.WriteMessage($"\n[LayerPdfExport] Deleted {deleted}/{nonTargetLayouts.Count} non-target layout(s).\n");

        string safe = SanitizeFileName(layoutName);
        string dwgPath = Path.GetFullPath(Path.Combine(dwgDir, $"{safe}.dwg"));
        if (File.Exists(dwgPath))
            File.Delete(dwgPath);
        ed.Command("._-WBLOCK", dwgPath, "*");

        if (!File.Exists(dwgPath))
            throw new InvalidOperationException($"-WBLOCK did not produce {dwgPath}");
        ed.WriteMessage($"\n[LayerPdfExport] Layout \"{layoutName}\" -> {dwgPath}\n");

        string zipPath = Path.Combine(Directory.GetCurrentDirectory(), "layout_dwgs.zip");
        if (File.Exists(zipPath))
            File.Delete(zipPath);
        ZipFile.CreateFromDirectory(dwgDir, zipPath);
        ed.WriteMessage($"\n[LayerPdfExport] Wrote {zipPath}\n");
    }

    /// <summary>
    /// Lightweight variant: one DWG per layout via <see cref="ExportLayoutViaWblock"/> (paperspace
    /// entities only, no model space). Zipped to <c>layout_dwgs.zip</c>.
    /// </summary>
    [CommandMethod("ExportAllLayoutDwgsWblock", CommandFlags.Modal)]
    public static void ExportAllLayoutDwgsWblock()
    {
        var doc = Application.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("No active document.");
        var db = doc.Database;
        var ed = doc.Editor;

        ed.WriteMessage("\n[LayerPdfExport] ExportAllLayoutDwgsWblock — paperspace-only, one DWG per layout.\n");
        SetAllLayersOffState(db, null, false);

        string dwgDir = Path.Combine(Directory.GetCurrentDirectory(), "_layoutdwg_out");
        if (Directory.Exists(dwgDir))
            Directory.Delete(dwgDir, true);
        Directory.CreateDirectory(dwgDir);

        ed.Command("._FILEDIA", "0");

        var layouts = GetPaperLayoutNamesOrdered(db);
        if (layouts.Count == 0)
        {
            string src = db.Filename;
            if (string.IsNullOrWhiteSpace(src) || !File.Exists(src))
                src = doc.Name;
            string modelPath = Path.GetFullPath(Path.Combine(dwgDir, "model.dwg"));
            File.Copy(src, modelPath, overwrite: true);
        }
        else
        {
            foreach (string layoutName in layouts)
            {
                string safe = SanitizeFileName(layoutName);
                string dwgPath = Path.GetFullPath(Path.Combine(dwgDir, $"{safe}.dwg"));
                ExportLayoutViaWblock(db, layoutName, dwgPath);
                ed.WriteMessage(File.Exists(dwgPath)
                    ? $"\n[LayerPdfExport] Layout \"{layoutName}\" -> {dwgPath}"
                    : $"\n[LayerPdfExport] Warning: no DWG for layout \"{layoutName}\"");
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

    private static Dictionary<string, List<ObjectId>> BuildLayoutEntityMap(
        Database db, List<string> layoutNames)
    {
        var map = new Dictionary<string, List<ObjectId>>(StringComparer.OrdinalIgnoreCase);
        using var tr = db.TransactionManager.StartTransaction();
        var dict = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
        foreach (string name in layoutNames)
        {
            var ids = new List<ObjectId>();
            if (dict.Contains(name))
            {
                ObjectId layId = dict.GetAt(name);
                var layout = (Layout)tr.GetObject(layId, OpenMode.ForRead);
                var btr = (BlockTableRecord)tr.GetObject(layout.BlockTableRecordId, OpenMode.ForRead);
                foreach (ObjectId oid in btr)
                    ids.Add(oid);
            }
            map[name] = ids;
        }
        tr.Commit();
        return map;
    }

    private static void EraseObjectIds(Database db, List<ObjectId> ids)
    {
        using var tr = db.TransactionManager.StartTransaction();
        foreach (var oid in ids)
        {
            if (!oid.IsValid || oid.IsErased) continue;
            var obj = tr.GetObject(oid, OpenMode.ForWrite);
            obj.Erase();
        }
        tr.Commit();
    }

    private static void UnEraseObjectIds(Database db, List<ObjectId> ids)
    {
        using var tr = db.TransactionManager.StartTransaction();
        foreach (var oid in ids)
        {
            if (!oid.IsValid) continue;
            var obj = tr.GetObject(oid, OpenMode.ForWrite, openErased: true);
            obj.Erase(false);
        }
        tr.Commit();
    }

    /// <summary>
    /// Extract a layout's paperspace entities via <see cref="Database.Wblock(ObjectIdCollection, Point3d)"/>.
    /// No <c>HostApplicationServices.WorkingDatabase</c> switching, no secondary <c>LayoutManager</c>
    /// calls — avoids the native AV that occurs in AcCoreConsole/DA when those APIs are used.
    /// Content lands in model space of the new DWG (same as <c>-EXPORTLAYOUT</c>).
    /// </summary>
    private static void ExportLayoutViaWblock(Database sourceDb, string layoutName, string dwgPath)
    {
        ObjectIdCollection ids;
        using (var tr = sourceDb.TransactionManager.StartTransaction())
        {
            var dict = (DBDictionary)tr.GetObject(sourceDb.LayoutDictionaryId, OpenMode.ForRead);
            if (!dict.Contains(layoutName))
                throw new InvalidOperationException($"Layout not found: {layoutName}");
            ObjectId layId = dict.GetAt(layoutName);
            var layout = (Layout)tr.GetObject(layId, OpenMode.ForRead);
            var btr = (BlockTableRecord)tr.GetObject(layout.BlockTableRecordId, OpenMode.ForRead);

            ids = new ObjectIdCollection();
            foreach (ObjectId oid in btr)
                ids.Add(oid);
            tr.Commit();
        }

        if (ids.Count == 0)
            return;

        using var newDb = sourceDb.Wblock(ids, Point3d.Origin);
        newDb.SaveAs(dwgPath, DwgVersion.Newest);
    }

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

    private static void CommandExportPdf(Editor ed, bool paperLayout, string pdfPath)
    {
        if (paperLayout)
            ed.Command("._-EXPORT", "PDF", "Current", "No", pdfPath);
        else
            ed.Command("._-EXPORT", "PDF", "Extents", "No", pdfPath);
    }

    private static string PointToCmd(Point3d p) =>
        $"{p.X.ToString(CultureInfo.InvariantCulture)},{p.Y.ToString(CultureInfo.InvariantCulture)}";

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

    private static void ActivatePaperLayout(Database db, string layoutName, int waitMs = 150)
    {
        db.TileMode = false;
        LayoutManager.Current.CurrentLayout = layoutName;
        if (waitMs > 0)
            Thread.Sleep(waitMs);
    }

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

    private static void MakeAllLayersVisible(Database db)
    {
        using var tr = db.TransactionManager.StartTransaction();
        var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
        foreach (ObjectId id in lt)
        {
            var ltr = (LayerTableRecord)tr.GetObject(id, OpenMode.ForWrite);
            ltr.IsOff = false;
            ltr.IsLocked = false;
        }
        tr.Commit();
    }

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

    /// <summary>
    /// Thaws all layers in all viewports of the named layout using the VPLAYER command.
    /// Must be called while the target layout is already the active layout (paperspace).
    /// </summary>
    private static void ClearViewportLayerFreezes(Editor ed)
    {
        // VPLAYER Thaw * in all viewports.
        // Prompt sequence: option → layer pattern → viewport selection ("A" = All).
        try
        {
            ed.Command("._VPLAYER", "T", "*", "A", "");
        }
        catch (System.Exception ex)
        {
            ed.WriteMessage($"\n[LayerPdfExport] Warning: VPLAYER thaw failed: {ex.Message}\n");
        }
    }

    private static string SanitizeFileName(string name) =>
        Regex.Replace(name, @"[^\w\.\-]", "_", RegexOptions.None, TimeSpan.FromSeconds(1));

    /// <summary>
    /// Returns the axis-aligned bounding boxes (in model space coordinates) of every
    /// non-overall viewport found in the named layout. Accounts for ViewCenter, ViewHeight,
    /// Width/Height aspect ratio, and ViewTwist. A 5 % buffer is added around each extent
    /// so that entities sitting exactly on a viewport boundary are not accidentally clipped.
    /// </summary>
    private static List<Extents3d> GetViewportModelSpaceExtents(Database db, string layoutName)
    {
        var extents = new List<Extents3d>();
        using var tr = db.TransactionManager.StartTransaction();
        var dict = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
        if (!dict.Contains(layoutName)) { tr.Commit(); return extents; }
        var layout = (Layout)tr.GetObject(dict.GetAt(layoutName), OpenMode.ForRead);
        var btr = (BlockTableRecord)tr.GetObject(layout.BlockTableRecordId, OpenMode.ForRead);
        foreach (ObjectId id in btr)
        {
            var vp = tr.GetObject(id, OpenMode.ForRead) as Viewport;
            if (vp == null || vp.Number == 1) continue; // skip the overall paper viewport
            double cx = vp.ViewCenter.X, cy = vp.ViewCenter.Y;
            double halfH = vp.ViewHeight / 2.0;
            double halfW = halfH * (vp.Width / vp.Height);
            double twist = vp.TwistAngle;
            double cosT = Math.Cos(twist), sinT = Math.Sin(twist);
            // Rotate all four corners and compute their AABB.
            double[] dxs = { -halfW,  halfW,  halfW, -halfW };
            double[] dys = { -halfH, -halfH,  halfH,  halfH };
            double minX = double.MaxValue, maxX = double.MinValue;
            double minY = double.MaxValue, maxY = double.MinValue;
            for (int i = 0; i < 4; i++)
            {
                double rx = cx + dxs[i] * cosT - dys[i] * sinT;
                double ry = cy + dxs[i] * sinT + dys[i] * cosT;
                if (rx < minX) minX = rx; if (rx > maxX) maxX = rx;
                if (ry < minY) minY = ry; if (ry > maxY) maxY = ry;
            }
            double bx = (maxX - minX) * 0.05;
            double by = (maxY - minY) * 0.05;
            extents.Add(new Extents3d(
                new Point3d(minX - bx, minY - by, double.MinValue / 2),
                new Point3d(maxX + bx, maxY + by, double.MaxValue / 2)));
        }
        tr.Commit();
        return extents;
    }

    /// <summary>
    /// Returns ObjectIds of model space entities whose bounding box does not intersect
    /// any of the supplied <paramref name="keepExtents"/>. Those entities belong to a
    /// different floor plan and should be erased before exporting the current layout.
    /// Entities with no computable extents (XLines, Rays, etc.) are conservatively kept.
    /// </summary>
    private static List<ObjectId> CollectModelSpaceEntitiesToErase(
        Database db, List<Extents3d> keepExtents)
    {
        var toErase = new List<ObjectId>();
        using var tr = db.TransactionManager.StartTransaction();
        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
        foreach (ObjectId id in ms)
        {
            if (id.IsErased) continue;
            var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
            if (ent == null) continue;
            bool keep = false;
            try
            {
                var ext = ent.GeometricExtents;
                keep = keepExtents.Any(vp =>
                    ext.MinPoint.X <= vp.MaxPoint.X && ext.MaxPoint.X >= vp.MinPoint.X &&
                    ext.MinPoint.Y <= vp.MaxPoint.Y && ext.MaxPoint.Y >= vp.MinPoint.Y);
            }
            catch (Autodesk.AutoCAD.Runtime.Exception) { keep = true; }
            if (!keep) toErase.Add(id);
        }
        tr.Commit();
        return toErase;
    }
}
}
