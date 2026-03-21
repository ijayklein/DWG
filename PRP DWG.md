Here is a complete Product Requirements Document (PRD), formatted specifically as a **Product Requirements Prompt (PRP)**.

You can copy and paste this entire block directly into an AI coding assistant (like Cursor, GitHub Copilot, or ChatGPT) or hand it to a human developer. It clearly defines the zero-touch automation logic, the technical stack, and the exact rendering constraints we discussed.

------

## Product Requirements Prompt (PRP): Automated CAD-to-PDF Floor Plan Extractor

**Context for AI/Developer:**

Act as a Senior Python Developer and CAD Automation Engineer. Your task is to build a fully automated, zero-touch pipeline that ingests a master CAD file (DXF) containing multiple floor plans scattered across Model Space, algorithmically identifies each discrete floor plan, and exports each one as a separate PDF file. The original CAD layer structure **must** be preserved in the final PDFs.

### 1. Objective and Scope

- **Goal:** Eliminate manual CAD drafting tasks (no manual boundary drawing, no manual plotting). The user should be able to drop a multi-plan DXF into an input folder, run the script, and receive isolated PDFs in an output folder.
- **Target OS:** macOS (Apple Silicon or Intel).
- **Core Technologies:** Python 3, `ezdxf` (for parsing and bounding box math), QCAD Command Line / `dwg2pdf` (for rendering and layer preservation).

### 2. Functional Requirements

#### A. File Ingestion

- The script must monitor or accept a specific input directory for `.dxf` files.
- The script must safely handle file parsing using `ezdxf`, gracefully skipping corrupted files and logging errors.

#### B. Algorithmic Boundary Detection (Zero-Touch)

The system must not rely on the user manually drawing rectangles. Implement one (or a configurable combination) of the following detection strategies:

| **Strategy**              | **Description**                                              | **Technical Implementation**                                 |
| ------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| **1. Block Recognition**  | Finds standardized blocks (like Title Blocks or Frames) surrounding each plan. | Query `ezdxf` for specific `INSERT` entities by block name. Extract scale and insertion point to calculate the `[min_x, min_y, max_x, max_y]` bounding box. |
| **2. Spatial Clustering** | Identifies clusters of CAD geometry separated by empty white space. | Use `scikit-learn` (DBSCAN) or custom bounding math to group all lines/polylines into discrete clusters, drawing a virtual bounding box around each group with a 5% margin. |
| **3. Layout Extraction**  | Detects if plans are already separated into Paper Space Layouts. | Bypass boundary math and trigger QCAD's `-a` flag to plot all existing layout tabs. |

#### C. Processing & PDF Rendering

- Once bounding boxes `[minX, minY, maxX, maxY]` are calculated for each floor plan, the script must pass these coordinates to the macOS QCAD command-line tool.
- **Executable Path:** Must be configurable, defaulting to the macOS standard: `/Applications/QCAD-Pro.app/Contents/Resources/dwg2pdf` (or standard QCAD app path).
- **Rendering Command:** The script must construct and execute the subprocess command using the `-window` argument.
- **Layer Preservation:** The rendering engine must natively translate DXF layers to PDF layers (inherent to QCAD, but must not be overridden by flattening flags).

### 3. Non-Functional Requirements

- **Error Handling:** If a coordinate window contains no printable geometry, skip and log it. If QCAD fails to execute due to macOS Gatekeeper restrictions, surface a clear terminal message instructing the user to allow the app in System Settings.
- **Performance:** The script should process files asynchronously or sequentially without freezing the host machine. Large ASCII DXF files (50MB+) must be handled efficiently.
- **Output Naming:** PDFs must be sequentially named (e.g., `OriginalFileName_Plan01.pdf`, `OriginalFileName_Plan02.pdf`) or named based on text extracted from the Title Block if Strategy 1 is used.

### 4. Edge Cases to Handle

- **Overlapping Geometries:** If rogue lines connect two floor plans, the clustering algorithm might merge them. The script should allow an optional "max bounding box size" to flag anomalies.
- **Empty Model Space:** If the script detects zero entities in Model Space, it should check Paper Space layouts before failing.

