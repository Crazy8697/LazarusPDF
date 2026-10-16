# Lazarus manual

The full feature reference and developer notes, stage by stage. For installing, see the README. Everything runs on this computer; Lazarus writes no logs.

## Run
- Installed: the Start-menu or desktop shortcut **Lazarus**. From source: `Lazarus.bat` in the source folder (uses `.venv` there, or the venv in `LAZARUS_VENV`).
- Headless: `<venv>\Scripts\python.exe -m lazarus --cli IN.pdf OUTDIR`. Run it from the source folder. It prints counts only.

## What it does
For each page it reads the PDF's text layer first. Pages with no usable layer go to OCR (Tesseract, English, deskew, column-aware).
Ink that the layer doesn't cover is either kept as a figure or OCR'd. A gap means no text layer and OCR that's low-confidence or garbled.
Lazarus never guesses at gap content, and it never modifies the original.

Outputs go to the output folder (default `%USERPROFILE%\extract-out`), using the source's base name:
`-review.html` (gaps highlighted, figures embedded, prev/next gap buttons), `-text.txt`, `-final.pdf`, `-gaps.txt`.

## One window, two modes
Lazarus has a single main window with a **MODE** switcher at the top left: **PDF Editor** | **Extraction**.
You can also switch from the **Mode** menu (between File and View), with Ctrl+Shift+1 or with Ctrl+Shift+2.
Switching swaps the central area and shows only that mode's toolbars, side panels and menus.
The hidden mode keeps its state (open PDF, page, zoom, unsaved edits), and its shortcuts are off.

- At launch the window opens in PDF Editor mode with nothing over it. The import pop-up only opens when you ask for it.
- The editor's MARKUP, EDIT and INSERT tool groups can be dragged by their dotted handle to reorder them. The order holds for the session.
- **File › Settings…** (Ctrl+,) opens the Settings page (see below). **File › About Lazarus** shows the version.
- **File › Import PDF…** (Ctrl+I) is for extraction. The result lands in Extraction mode.
- **File › Open PDF in Editor…** (Ctrl+O) is for viewing. The PDF opens in PDF Editor mode.
- Dropping a PDF on the window opens it in the editor when you're in editor mode, and stages it in the import pop-up when you're in extraction mode.

### Layout
- **Modes:** Overview (Ctrl+Shift+1) | PDF Editor (Ctrl+Shift+2) | Extraction (Ctrl+Shift+3).
- **Tool groups (MARKUP / EDIT / INSERT):** drag a group by its dotted handle.
  - Along row 2 it reorders.
  - Up onto row 1 it turns compact (title + icons only); back down it returns to full form.
  - To the window's left or right edge it becomes a vertical sidebar.
  - If row 2 empties it collapses. Dragging a group back below row 1 shows a "Drop here to open row 2" target.
- **View menu:** Markup / Edit / Insert toolbar toggles, Pages / Document panel toggles, and **Lock layout**, which freezes every toolbar and panel.
- **Panels:** the Pages and Document panels can be docked at the top (or bottom). They re-flow horizontally there.
- **Overview:** every page in a zoomable grid. It shows real pages if a PDF is open in the editor, otherwise numbered empty pages.
  - Row 2 has the pages-wide × pages-high boxes, presets 30×10 / 15×5 / 8×3, and the page operations.
  - You can't zoom out past about 30 × 10 pages.

## Extraction mode
The central area is an editable review pane. **File › Import PDF…** (Ctrl+I) opens the import pop-up, which also opens by itself at launch.
You can also drop a PDF anywhere on the window. The pop-up closes on its own when extraction finishes, and the result loads into the editor.

- Every bit of text is editable.
- Open gaps are yellow with red text. F3 / Shift+F3 (or Next gap / Prev gap) selects the next open gap; type to replace it.
- Clicking into an open gap selects the whole marker.
- A gap whose text no longer reads `[[FILL IN — p.N]]` counts as filled and turns green with a dotted underline. To change the colours, edit `STYLES` in `lazarus/editor.py`.
- **Save** (Ctrl+S) rewrites the four review outputs in place. It never writes copies.
- **Export ▾** saves PDF, Word (.docx), EPUB, Markdown, HTML or .txt, always from the current edited text. It also makes the two PDF outputs below: **Searchable PDF** and **Rebuilt pages**.
  - Filled gaps export as plain text. Open gaps keep the highlighted marker.
  - Images go into PDF, DOCX, EPUB and HTML. Markdown and .txt keep `[[FIGURE — p.N]]`.
  - If any gaps are still open, you're asked to confirm the export first.
  - Page headings are off in exports by default (toggle under Export).

## PDF Editor mode (stage 1 UI shell)

What works:
- A read-only page canvas (QGraphicsView) with zoom in/out, typed zoom %, Ctrl+wheel, fit width (Ctrl+0), 100% (Ctrl+1), pan, PgUp/PgDn, and a go-to-page box.
- A thumbnail sidebar; click a thumbnail to jump to that page.

Every tool is live (the sections below). The PDF is never written.
The tool list lives in `TOOLS` in `lazarus/pdfeditor.py`. Icons come from QtAwesome (Material Design Icons).

## PDF Editor: editing and saving
- **Originals are never written.** The PDF is read into memory and edited there; the file on disk stays exactly as it is.
  Every PDF Lazarus opens (editor or extraction import) is registered as protected (`lazarus/protect.py`). Every write path checks it: editor saves, review files, all exports. Picking an original is refused with a message, and you're asked to choose another file. There is no backup folder and no overwrite option.
- **Undo / redo:** PyMuPDF journalling. Every edit is one operation. Ctrl+Z / Ctrl+Y (or Ctrl+Shift+Z), also under Edit.
- **Selection:** in Select mode, click-drag on a page selects text. Hit-testing uses PyMuPDF character geometry and works on rotated pages too. Ctrl+C copies.
  Edit › *Highlight selection (pipeline test)* is the one real edit until the markup tools arrive.
- **Saving:** the first Ctrl+S asks for a new file (suggested `<name>-edited.pdf` next to the original); later saves go there. **Save PDF As…** (Ctrl+Shift+S) picks another new file.
  Writes are atomic (temp file, then replace). If the target is locked by another program, you get an error and nothing is half-written.
- **Unsaved edits:** the title shows "• unsaved PDF edits". Opening another PDF or quitting asks Save / Discard / Cancel.

## Page operations (stage 6)
**Overview is their home.** The same actions appear in the Overview PAGES group, the editor's Pages panel and the Page menu (visible in both modes).
- **Selection.** In Overview: click, Ctrl+click (toggle), Shift+click (range), Ctrl+A (all); click empty space to clear. Selected tiles are outlined in blue.
  In the editor: the thumbnail selection, or the current page if nothing is selected. Buttons enable only when the op makes sense for the selection.
- **Every op that changes the document is one undoable step** on the in-memory working copy. Ctrl+Z / Ctrl+Y work in Overview too, and the Page menu has Undo/Redo. After each op or undo, the canvas, thumbnails, Overview grid, page count and page navigation all rebuild, keeping zoom and position.

The nine ops:
- **Reorder:** drag selected tiles in Overview (a blue bar marks the drop point), or drag thumbnails. The Reorder button moves the selection to before page N.
- **Rotate:** right (default), left or 180°, from the button's menu.
- **Delete:** asks first. Deleting every page is refused.
- **Insert:** a blank page sized like its neighbour, or all pages of another PDF, before or after the selection. Pages from another PDF bring their annotations, links between them and form fields (stage 17).
- **Duplicate:** copies go right after their originals.
- **Extract:** writes the selected pages to a new PDF, keeping links between them. The working copy is unchanged.
- **Merge:** adds all pages of one or more PDFs, at the end or before/after the selection, with their annotations, links and form fields.
- **Split:** every N pages, or after each selected page, into `<name>-part-NN.pdf` in a folder you choose. The working copy is unchanged.
- **Crop:** drag a box on a page in the editor (Esc cancels), then apply it to the selected pages, that page, or all pages. Crop hides content; it doesn't delete it.
  If those pages are not all the same size, it asks how: **Same margins** (every page loses the same distance from each edge), **Same proportion** (every page keeps the same share of its width and height) or **Same box** (the drawn box as-is). One undo step either way.

PDFs used for Insert/Merge are opened read-only and registered as protected. Extract and Split go through the same never-write-an-original check as Save.
Insert/Merge read the other PDF in the background (`loader.py`) with a progress dialog and Cancel, then graft its pages in 50-page parts that make ONE undo step; the window keeps painting throughout and Cancel at any point adds nothing. (One MuPDF journal operation slows down sharply with thousands of new objects: a 1,500-page insert took about 45 s as one operation and about 1 s in parts.)
Implementation note: PyMuPDF 1.28's `new_page()`/`insert_pdf()` refuse to run on a journalled document, so `pageops.py` uses the MuPDF calls they wrap (`pdf_add_page`/`pdf_insert_page`, `pdf_graft_mapped_page`) inside the journal operation.
## Bookmarks (stage 7)
- A **Bookmarks** panel sits in a tab next to Pages, in the editor and in Overview. It's movable, has a View toggle, and is frozen by Lock layout.
  It shows the outline as a tree with its nested levels. Clicking a bookmark jumps the editor to its page and highlights that page in Overview.
- **Editing:**
  - *Bookmark this page* (Ctrl+B, also in the Page menu) bookmarks the current page, or in Overview the selected page. It goes after the current bookmark, and you type its name straight away.
  - Rename with F2.
  - Delete removes the bookmark and everything nested under it.
  - Drag to reorder, or drop onto another bookmark to nest under it.
  - *Point at current page* / *Set target page…* change the target.
- Every edit is one undoable step (Ctrl+Z / Ctrl+Y), sets the unsaved flag, and goes out with the next save, which is always a new file.
- **Through page ops:** destinations point at page objects, so Move / Insert / Duplicate keep each bookmark on its page; Rotate and Crop don't touch bookmarks.
  - Delete removes bookmarks to deleted pages; their nested bookmarks move up to take their place.
  - Merge brings each merged PDF's bookmarks in under a new top-level bookmark named after that file.
  - Extract and Split give the new files the bookmarks for their pages, renumbered.
  - Undo restores the outline exactly.
- Implementation note: PyMuPDF's `set_toc()` can't run inside a journal operation (`get_new_xref()`/`update_object()` refuse), so `outline.py` writes the outline with MuPDF primitives inside the op.
## Auto-generated bookmarks (stage 7a)
- **Auto-generate bookmarks…** (the Bookmarks panel's toolbar) proposes an outline for the open PDF, shown in an editable preview before anything changes. The core (`autobookmarks.py`) has no UI, so Batch can reuse it.
- **Headings** (default): the body text size is the size most characters use; larger lines (or bold at body size) are heading candidates. Their styles are ranked, largest first, and the ticked ones become levels 1, 2, 3… Running headers / footers, page numbers and long lines are ignored; a heading over two lines becomes one title; each bookmark lands on its heading's line. Set how many levels and the size threshold, or tick and untick individual styles, and the tree updates.
- **Table of contents page**: offered when a page early in the PDF looks like one ("2.1 Fuses …… 17"). Levels come from the numbering (2 / 2.1 / 2.1.3) or the indent; printed page numbers are mapped to real pages by finding the titles in the document.
- In the preview: rename (double-click), drag to reorder or re-nest, Remove, Indent, Outdent. If the PDF already has bookmarks: Replace (default), add after them, or nest the new ones under a "Generated bookmarks" entry.
- Apply is one undo step, written the same way as hand-made bookmarks (journal-safe outline writer; PyMuPDF's set_toc is a snapshot op and isn't used on the working copy), so every stage-7 action works on the result and the bookmarks follow their pages.
- Best effort: good on consistent documents (manuals), weaker on messy ones. A scan without a text layer has nothing to find; it says so and suggests OCR to searchable first.

## Find (stage 8)
- **Ctrl+F** (also Edit › Find and View › Find) opens a find bar along the bottom, in the editor and in Overview. Esc closes it.
- Hits update live: "Searching… N" while it runs, then "3 of 17". Enter or F3 goes to the next hit; Shift+Enter or Shift+F3 goes back. Both wrap at the ends.
- Options: **Match case**, **Whole word**, **Regex** (a bad regex shows "Bad regex"). Spaces in a query match any whitespace, so phrases match across line breaks.
- Every hit gets a translucent yellow box; the current hit is orange. These are view-only scene items, not annotations, so they never touch the document, the unsaved flag or anything saved or exported.
  Jumping goes to the hit's page and scrolls it into view. In Overview, the hit's page is selected and scrolled to.
- **Read-only by design.** Text comes from one plain in-memory snapshot of the working copy, rebuilt only when the document changes; pages are indexed lazily and cached.
  The journalled document is never read for text. PyMuPDF would briefly modify rotated or annotated pages to extract text, which would count as an edit.
  Matching is done in `search.py` over PyMuPDF's per-character geometry, because `page.search_for()` is always case-insensitive and has no whole-word option.
- Hits refresh after page ops, undo/redo and edits. On a 400-page dense book, the first search (including indexing) took about 2.3 s with the UI responsive throughout (longest stall about 80 ms); later searches take about 30 ms.
- **Index builds** (the first search of a PDF, or reaching pages not indexed yet): if still running after 250 ms, the count reads **Indexing X/Y** (pages indexed so far, climbing as it goes) and the rest of the window is locked (widgets and keyboard shortcuts, tab switching included) so stray input can't queue. Only the query field and **Cancel** stay live, plus the window's close button, which cancels the build first and then asks about unsaved tabs as usual. Small, fast builds just finish, with no lock and no flash. A fully indexed PDF never locks anything.
- **Cancel** (or Esc) stops between slices and keeps what's indexed: the count reads like "3 of 12+ · Partial (120/400)", and Enter/F3 step through those hits. A second Esc closes the bar. The next search re-matches the indexed pages from cache and indexes on from the next page; when it completes it's a normal full result. Changing the query mid-build restarts matching without redoing any indexing. Any edit to that tab's PDF discards its index, partial or full.

## Password-protected PDFs
- Opening one (Ctrl+O, drag-drop, Extraction import, Insert/Merge) asks for its password in a masked prompt. A wrong password asks again; Cancel opens nothing. Files that only restrict permissions (no open password) open without asking.
- **Saves keep the protection by default** ("Keep password"): AES-256 encryption (an RC4 or AES-128 source is upgraded), same permission flags, and the password you typed opens the copy. Extract and Split write with the document's current choice too.
  PyMuPDF writes an *unencrypted* file on a normal save even from an encrypted source, so `locked.py` passes the encryption explicitly on every write.
- **File › Save With Password** has four choices. Each writes a NEW file (the original is never touched), and plain Ctrl+S then quietly repeats that choice:
  - **Keep Password…**: carry the open password forward. If the file was unlocked with its owner password, that stays the owner password.
    If the owner password is unknown (unlocked with the open password, or a permission-only file), the copy would get a random owner password, so a warning says nobody could ever change its permissions and recommends Set passwords: **Set passwords instead** or **Continue anyway**. On plain Save As this warning shows once per document.
  - **Set Passwords…**: Open password and Owner password; blank means not set. Both = open password to open, owner password controls permissions. Open only = password to open, no permission lock. Owner only = opens freely, permissions restricted. Both blank = unprotected.
    Six permission boxes (printing, high-res printing, copy/extract text, edit content, annotate, fill forms), all allowed by default, apply only with an owner password and stay greyed until one is typed. New passwords use AES-256.
  - **Match Passwords…**: one password (typed twice) is both the open and the owner password.
  - **Clear Passwords…**: warns, then writes the file with no password and no restrictions.
- The status bar shows what saves will write: "saves: password-protected", "… (new passwords)", "permission-protected" or "no password".
- PyMuPDF gotcha: never read `doc.needs_pass` after `authenticate()`; it resets the decryption and page streams then fail to decode.
- Passwords live in memory for the session only; never saved, logged or shown. Extraction outputs (review files and the six exports) are new derived files and are not password-protected.
## Tabs: several PDFs open at once
- Every PDF opens in its own tab above the canvas (Ctrl+O can pick several; dropping several PDFs opens each; PDFs given on the command line open as tabs). Opening a PDF that is already open switches to its tab.
- **Big PDFs load in the background** (`loader.py`): the tab opens at once showing real progress (MB read, then pages prepared; an animated bar while PyMuPDF parses), the window stays responsive, and the tab fills in when ready. Several can load at once; closing a tab mid-load cancels it cleanly. Pages and thumbnails then render a page per event-loop turn, visible pages first, so heavy pages never stall the window in a burst. (PyMuPDF holds Python's GIL inside each call, so work is split into many short calls rather than relying on the thread alone.)
- Each tab is a Session (`tabs.py`) with its own working copy and undo history, unsaved flag (● on the tab), protected-original registration, canvas (zoom, page, text selection), find index, and the side panels' state (thumbnail selection and scroll, Overview selection, bookmark tree). Switching tabs restores all of it; the thumbnails, bookmarks, Overview, page ops and find bar follow the active tab.
- **Close Tab** (Ctrl+W / Ctrl+F4, or the tab's ×) asks Save / Discard / Cancel only if that tab has unsaved edits. Closing the window asks for every unsaved tab. Ctrl+Tab / Ctrl+Shift+Tab cycle tabs.
- The snapshot-undo memory budget is shared by all tabs. Under pressure, redo steps held by OTHER tabs are dropped first, then the oldest undo states.
- **View › Compare Side by Side** (Ctrl+Alt+C) splits the central area: the active tab on the left, any open PDF (or the same one again) on the right, with **Synced scrolling** (same page, same position) on by default. The differences are marked by Compare (stage 13); pages can be dragged between the panes (stage 17).
- The tab bar also shows in **Overview**: the same tabs, dirty dots, switching, Close Tab and Ctrl+Tab; Overview shows the active tab's pages.

## Pages between tabs (stage 17)
- Select pages in the thumbnails or Overview, then **Ctrl+C** (copy), **Ctrl+X** (cut), and **Ctrl+V** in any open tab: the pages go after the selected page (or the current page). **Page › Paste pages before / after** and the page right-click menu pick the side. The keys act on pages while the thumbnails or Overview have the focus; elsewhere Ctrl+C still copies selected text.
- **Drag** pages from the thumbnails or Overview onto another tab: hovering a tab switches to it, and the drop shows the blue insertion bar in the thumbnails or Overview; dropping on the tab itself adds them at its end. A drag is a copy (the source tab is unchanged); within the same tab it is a reorder as before.
- **Compare view:** Alt+drag a page in one pane onto the other pane (a blue line marks the spot), or drag thumbnails onto either pane.
- What travels: page content, size and rotation, annotations (every markup and page-content tool, with their editable roles), links whose target is among the copied pages (others are dropped), and form fields. A field whose name is already used in the destination is renamed ("Name (2)") so the two stay separate; a message lists the renames once per paste. **Page › Bring bookmarks when pasting** (off by default) also brings the bookmarks that point at the copied pages.
- **Undo:** a paste is ONE undo step in the tab it lands in; a cut is ONE undo step in the tab it came from. The two histories are independent.
- The clipboard is internal and holds page data, not a file path: the source tab can be edited or closed afterwards. **File › Paste Pages as New PDF** opens the copied pages as a new, unsaved tab.
- Pages of a PDF whose permissions don't allow copying (owner password not known) can't be copied, cut or dragged out; it says why.
- **Right-click** a page in the Pages panel or Overview: Copy, Cut, Paste before / after, Duplicate, Rotate left / right, Delete, Extract, Insert blank page, Insert from file, Insert images, Insert scan.
- Engine: `pageclip.py` grafts pages with MuPDF primitives inside the journal, and grafts each annotation through the same graft map (MuPDF's page graft leaves /Annots behind), then points links at the new pages and registers form fields in the AcroForm. Declared and probed in `oprules.py` as `paste_pages` / `cut_pages` (journal).
- Extraction mode stays single-document.

## Annotations (stage 9a)
- The **MARKUP** tools work: Highlight, Underline, Strike (drag across text), Draw (freehand ink), Shapes (rectangle, ellipse, line, arrow, polygon; the arrow beside the button picks one; polygon: click the corners, double-click or Enter to finish), Text box (drag a box), Note (click), Stamp (click; the arrow picks Approved, Draft, Confidential and the rest). Signature arrives with 9b.
- Tools stay active for repeated use; Note, Text box and Stamp switch back to Select with the new annotation selected so you can edit it. Esc returns to Select (and cancels a half-drawn shape).
- **Select**: click an annotation to select it (Ctrl/Shift+click adds more on the same page), drag to move, drag a handle to resize, Del deletes. Clicking empty page still selects text. Highlight, underline and strikeout stay on their text: they can be restyled and deleted, not moved.
- **Properties** panel (tabbed with Document): colour, fill (or none), opacity, border width, line start/end, font and size and text colour for text boxes, text for text boxes and notes, author. With nothing selected it sets the defaults for the active tool's next annotation.
- Every annotation, move, resize, delete and property change is one undo step (Ctrl+Z / Ctrl+Y). All are PyMuPDF journal operations, declared and probed in `oprules.py` (including on a rotated page). An edit that fails halfway is rolled back and leaves nothing behind.
- Works on rotated pages: annotations land where they're drawn. Ink, lines and polygons are re-created with moved points when moved (PyMuPDF has no settable rect for them), keeping their style. No author is written unless you type one.

## Page-content tools (stage 9b)
- **Whiteout** (Edit group): drag a box; it is filled with the page's own background colour, sampled from the page right where you drop it (the dominant colour there, so text under it doesn't skew it: white, a cream scan, a coloured form box). Override the colour in Properties, or "Sample the page colour again". It covers content visually; it does not remove it (Redact, stage 10, does that).
- **Image** (Insert group): pick a picture, then drag a box (keeps its proportions) or click (its own size). PNG and JPEG go in as they are; WebP, GIF, BMP, TIFF and other raster formats are decoded by Pillow first; **SVG is rasterised** by MuPDF at its own size and 300 dpi, so it is crisp at its intended size, and is a bitmap once placed. Move / resize with the 8 handles; opacity in Properties.
- **Signature**: draw one (a signing pad) or import an image, then place it. Tick "Save to my signatures" to keep it for reuse (the button's menu lists saved ones; remove them there), or leave it unticked for a one-off. Saved signatures are PNGs in %APPDATA%\Lazarus\signatures on this computer only: never in the repo, never sent anywhere.
- **Measure**: Distance (drag) draws a dimension line whose caption is the reading; Area (click the corners, double-click or Enter to finish) draws the polygon with its reading label. The scale is the PDF's own when the page declares one (a /VP viewport /Measure), else a manual **Calibrate scale** (drag along a known distance, type its real length and unit; holds for every page without its own scale, for this session), else the paper size in mm. Moving keeps a reading; resizing re-measures. Each measurement also carries a standard /Measure dictionary.
- **Callout**: press on the spot to point at, drag to where the text box goes. A text box with a leader line and arrow; Properties sets box fill, border colour and width, text font, size and colour, the leader's end and the text. Moving or resizing the box keeps the tip on its spot.
- Every placement, move, resize, restyle and delete is one undo step. All 9b ops are journal-safe (probed on the worst-case files, including a rotated page, in `oprules.py`): none needed snapshot-undo.

## Document tools (stage 10)
- **Redact** (Edit group; its menu has the rest): mark areas (drag a box), text (drag across it) or every Ctrl+F hit (also on the find bar). Marks are annotations you can review, move, recolour (default black) or remove. **Apply redactions** asks first, then truly removes what is under every mark: text, image pixels, and any drawing a mark touches. It then re-extracts the text to verify nothing is left (and warns if anything is). One undo step.
- **Link** (Insert group, stage 18): drag a box over the spot, then pick where it goes: **a page in this PDF** (with the view on arrival: keep the reader's zoom, fit the page, fit the width, or a zoom percentage) or **a web address**. Links are invisible on the page, so while the Link tool is on, the links of the pages around the current one show as dotted outlines; click one to select it, drag to move, the handles resize, Del deletes, and **Properties** shows and edits its target (**Apply target**). Each change is one undo step (ops `link_add` / `link_edit`, journal-safe, probed on the 5 worst-case files). Links are standard /Link annotations: they survive Save, Extract and the stage 17 page copy / paste (a page link travels when its target page is copied too, and is dropped otherwise).
- **Form fields** (Insert group): with the Select tool, click a field to fill it (text, checkbox, radio, dropdown, list box). The button's menu adds text fields, checkboxes, radio buttons, dropdowns and list boxes (drag a box), and **Edit fields**: select, move, resize, delete, rename, set choices. Each change is one undo step.
- **Header / footer, Page numbers, Watermark, Bates numbers** (Document panel): a dialog with a live preview of any page; page ranges ("all", "1-3, 5, 8-"); text reads upright on rotated pages. Header/footer: six slots, {page} {pages} {date}. Page numbers: "1" / "Page 1" / "Page 1 of N", start number. Watermark: text or image (any format Insert image takes), diagonal / centre / corners, rotation, opacity, size, behind or in front. Bates: prefix, start, zero padding, suffix, position.
- **Metadata**: edit title, author, subject, keywords, creator, producer; see the dates; or **remove all metadata** (Info and XMP).
- **Password / encrypt**: the same menu as File › Save With Password. Every protected copy is AES-256 (Keep upgrades RC4 / AES-128 sources).
- **Flatten**: bake annotations (incl. whiteout, images, signatures, callouts, measurements) and/or form fields into the page content, for a range or the whole document. Fields left on other pages keep working. Redaction marks are not flattened. Whiteout still only covers; Redact removes.
- **Compress**: "Save compressed copy" writes a new file: garbage collection, compressed streams, optional image downsampling (DPI threshold / target, JPEG quality). Shows the size before and after; keeps the password choice; the working copy is not changed.
- **OCR to searchable** ("OCR page"): Tesseract adds an invisible text layer to scanned pages (page range, language, skip pages that already have text unless forced). Runs in the background with progress and Cancel; pages look the same; rotated scans line up; Find and selection work on the new text. One undo step.
- Undo paths (declared and probed in `oprules.py` on four worst-case files: with / without an Info dictionary, a rotated page, and text in a font no tool adds): journal for redaction marks + apply, forms, flatten; **snapshot** for text stamped into pages (header/footer, page numbers, Bates, watermark: PyMuPDF refuses a new font inside a journal operation), metadata edit / removal, and the OCR overlay (show_pdf_page is refused).

## Edit text and find / replace (stage 11)
- **Edit text** (EDIT group): point at text and its block is outlined; click it to edit it in place. A live editor sits over the block at its size and rotation. Ctrl+Enter (or a click outside) applies the edit as one undo step; Esc cancels.
- The text keeps its style per character: font, size, colour, bold / italic. Typed text takes the style next to it. The block reflows inside its own box, with the block's alignment and line spacing. If the new text doesn't fit, you choose: shrink it to fit, let the box grow downward, or keep editing. Nothing is ever cut off.
- Fonts:
  - The block's embedded font is reused when it can show every character (it is embedded again from its own program).
  - A standard font stays itself.
  - Otherwise the closest standard font is used (serif / sans / mono, weight, italic), and the editor says so before you apply, e.g. "Substitute font: Helvetica (the embedded font is a subset that lacks some of the characters)". Characters no available font has (e.g. Chinese) are refused, not mangled.
- The old text is really removed: a redaction per glyph, kept to a band inside the letter so neighbouring lines are untouched, with images and line art left alone. Pending redaction marks are set aside and put back. The page is then read again: if anything else moved, or the new text doesn't read back exactly, the edit is undone and you're told (and can choose to cover instead).
- Text that can't be edited in place (a Type3 font, text with no usable encoding, text at an angle) can be **covered**: a patch of the page's own colour, then the new text on top, with a clear notice that the old text is still in the file underneath. Redact is the tool that removes text.
- A scan has no text to edit: you're told, and OCR to searchable is suggested. The invisible OCR text layer is never edited as if it were visible text.
- If the PDF doesn't allow changing its content and the owner password isn't known, editing is blocked with the reason. Works on pages rotated 0 / 90 / 180 / 270.
- **Replace** (Ctrl+H) adds a Replace row under the find bar, using the same search: match case, whole word, regex, with `$1` / `\1` group references in the replacement.
  - **Replace** changes the current hit.
  - **Replace All…** (the whole document or a page range) first lists every change and every hit that will be skipped, with the reason: across two text blocks, invisible text, no font for the characters. It then applies them as ONE undo step, through the same write path as Edit text. Nothing is ever half-applied. The find count and highlights follow the change.
- Batch has a **Find and replace** step too (same engine; hits it can't replace are skipped and counted).

## Batch (stage 10b)
- **File › Batch…** (Ctrl+Shift+B): pick input PDFs (files, or a folder with or without its subfolders), an ordered list of operations, an output folder and a file-name pattern, then **Run batch**. The setup is remembered (never the passwords).
- Operations, run in the order listed on every file, each with its settings chosen up front (no questions mid-run): OCR to searchable, Redact search terms (marked, applied and verified; a file with text still under a mark is not written), Auto-generate bookmarks (headings method, best effort), Metadata (set / remove all), Flatten, Header / footer, Page numbers, Bates numbering (optionally continuing from file to file), Watermark (text or image), Extract text / export (Word, EPUB, Markdown, HTML, text or PDF, next to the PDF), Compress, Password / permissions. Stamping steps preview on a sample page.
- The job runs in **its own tab** (a gear icon: a job, not a document) while you keep working in the others, also while another tab's find index holds the window: progress bar, "X of Y files", a status line per file (queued / running / done / failed / cancelled / skipped, with the reason), **Cancel**, and a summary at the end. Closing a running job's tab asks first.
- Every result is a **new file** in the output folder, named by the pattern (`{name}`, `{n}`, `{date}`); an existing file is never replaced (`name (2).pdf`), inputs and open originals are never written. Writes are atomic, and a file that fails writes nothing at all. A protected input is opened with the batch password if one was given; outputs keep each file's own protection unless a Password step says otherwise (the same rules as Save With Password).
- One failed file never stops the batch. The work runs in a separate worker process, so the window never waits on it; a file that crashes the PDF engine is marked failed and a new worker carries on with the rest. Batch never edits an open document, so it adds no undo steps anywhere.
- New batchable operations plug in at one place each: `batch.OPS` / `batch.RUN` (what it does) and `batch_ui.fields_for` (its settings).

## Word export (stage 16)
- **File › Export › Word document (.docx)…** converts the open PDF to an editable Word file. It uses the PDF as it is now (unsaved edits included) and writes a NEW .docx, always at a location you choose. It uses pdf2docx: paragraphs, fonts where available, tables, images and basic layout come through; complex layouts may not convert perfectly.
- Options (remembered): page range, include images, detect tables, keep multi-column layout, and afterwards: show it in its folder / open it / nothing.
- Pages without text (scans) are noticed first. **Run OCR first** works on a temporary copy made for this export only (your PDF and its undo history don't change); those pages come through as text. **Convert anyway** brings them through as pictures.
- A PDF whose permissions forbid copying (opened without its owner password) can't be exported. A password-protected PDF gives a warning first, because a Word file can't carry the PDF's password.
- The conversion runs in a separate process with progress and Cancel. The .docx appears only when it is complete.
- Batch has a **Convert to Word** step too: a .docx per file, named by the batch pattern, with optional OCR first.

## Export: PDF, EPUB, Markdown, HTML, text (stage 18b)
- **File › Export** (and the Export button) from the PDF Editor: every entry is live. Each export uses the document as it is now (unsaved edits included), asks for a page range (and what to do afterwards: show it in its folder, open it, nothing), and writes a NEW file at a location you choose; an open original or protected file is refused (`protect.py`). It runs in a separate process (`docexport.py`) with progress and Cancel, and the file appears only when complete: a cancelled or failed export writes nothing.
- **PDF**: the working copy through the same writer as Save / Save As (`workdoc.write_pdf`) with the same password choice (including the warning when the owner password is unknown). Unlike Save As it doesn't become where Ctrl+S saves, and the document stays unsaved if it was. A page range keeps the links between the exported pages.
- **Plain text**: PyMuPDF's text per page in reading order (blocks top to bottom, left to right), with an optional "[Page N]" marker and a form feed between pages.
- **HTML**: ONE self-contained file; each page as PyMuPDF lays it out (positioned text) on a white sheet, images inlined as data URIs (no folder next to it).
- **Markdown**: headings from the stage 7a heading detection (font size / weight ranks; running headers left out), paragraphs from text blocks (a hyphen at a line end joins the word), bullet and numbered lists from their markers. Limits: no tables, no bold / italic inside paragraphs, images left out, multi-column pages may interleave.
- **EPUB** (EPUB 3, reflowable, EbookLib): chapters from the top-level bookmarks, else the detected headings, else every 10 pages; headings, paragraphs and lists as in Markdown; embedded images carried; a page with no text (a scan without OCR) comes in as a picture of the page. Limits: a chapter starts at the top of its page; tables and multi-column layouts flow as plain text; reading order is PyMuPDF's. Checked with W3C EPUBCheck at test time only (`tools/epubcheck_dev.py --jar <epubcheck.jar> FILE.epub`): Lazarus doesn't need Java or EPUBCheck, and neither is in the repo or the build.
- A PDF whose permissions forbid copying (opened without its owner password) can't be exported as text, HTML, Markdown or EPUB (the reason is shown); its PDF export follows the Save As rules. From a password-protected PDF the text formats warn first: they can't carry the password.

## Recent files and the Annotations panel (stage 18c)
- **File › Open Recent**: the 10 PDFs most recently opened in the PDF Editor, newest first, as a pick list (file names; the full path as the tooltip). Nothing is ever reopened by itself: there is no session restore. A file that is no longer there shows greyed (kept, in case its drive comes back); if one vanishes between showing the menu and clicking it, it is taken off the list with a quiet status message. **Clear recent files** sits at the bottom.
  - What feeds it: any PDF that opens as an editor tab, which is Open PDF (Ctrl+O), a PDF dropped on the window, a PDF given on the command line or handed over by another launch (double-clicking a PDF with Lazarus as the default reader), and Open Recent itself. Untitled documents (Paste Pages as New PDF, New PDF from Images, Scan) have no file and never count; neither does a PDF only read for Insert / Merge, nor an Extraction-mode import.
  - Stored in the user's own settings (QSettings key `recent_files`), like the rest of Settings.
- **Annotations panel** (View › Annotations panel; a tab next to Pages and Bookmarks): every annotation of the active tab with its page, type and text where it has any: the text under a highlight / underline / strikeout, a note's or text box's text, a callout's text, a measurement's reading, a stamp's name, a link's target. Click an entry to go to it: the view scrolls it into view and selects it (links with the Link tool on). It refreshes itself shortly after any change (an edit, undo / redo, a page op, another tab); while hidden it reads nothing. View only: no export.
  - Listed: highlight, underline, strikeout, drawing, rectangle, ellipse, line, arrow, polygon, note, text box, stamp, whiteout, image, signature, distance, area, callout, redaction mark, link. Not listed: form fields (fields, not annotations; Edit fields works with them), a note's popup (part of its note), an area's reading label (the reading shows on the area's entry).

## Scanning (stage 15)
- **File › Scan…** scans pages into a NEW PDF. It opens as a new tab, unsaved, like Images to PDF. **Insert › Scan as Pages…** scans into the open PDF at a position you pick (one undo step).
- Scanners are reached through Windows' own WIA (no TWAIN, nothing extra to install). Lazarus has its own dialog:
  - pick the scanner (the last one used is picked again; Refresh); if none is found it says why that usually happens
  - only what the scanner reports is offered: flatbed, feeder and duplex; colour, greyscale or black and white; its resolutions (300 dpi by default); page sizes that fit it; brightness and contrast if it has them
  - "Advanced…" opens the scanner's own Windows dialog for devices that need it
- Flatbed: **Scan**, then **Scan another page** as often as you like. Feeder / duplex: every page in one pass, ending when the feeder is empty (the normal end, not an error); duplex pages come front, back, front, back.
- Scanned pages collect as thumbnails: reorder (drag, Up / Down), rotate, delete, or rescan one. Optional, off by default: skip blank pages (with a threshold), straighten, crop to the content. Also: keep as scanned or recompress, and OCR to searchable afterwards. Settings are remembered.
- The scanner is worked from a background thread, so the window stays usable. Cancel stops after the current page. A paper jam, an open cover or a disconnected scanner stops the scan, keeps the pages already scanned, and says what happened.

## Images to PDF (stage 14)
- **File › New PDF from Images…**, or drop images on the window, makes a NEW PDF from images. It opens as a new tab, unsaved and marked as changed: nothing is written until you save it, to a file you choose. **Insert › Images as Pages…** adds images to the open PDF at a position you pick (one undo step).
- Formats: PNG, JPEG, WebP, GIF (first frame), BMP, TIFF (every page of a multi-page TIFF), other formats Pillow reads, and SVG (at its own size, rendered at 300 dpi). Phone photos come in upright (EXIF orientation). The images themselves are only ever read.
- The setup dialog:
  - lists the images in page order with thumbnails; drag or Up / Down to reorder, Remove, rotate each, sort by name, date taken or date modified
  - page size: each page fitted to its image, or Letter / Legal / A4 / A5 / custom, with orientation auto / portrait / landscape
  - placement: fit within the margins (default), fill (crop) or stretch, with margin and alignment
  - 1, 2, 4 or 6 images per page, with spacing
  - quality: keep the originals (JPEGs embedded byte for byte), or recompress (JPEG quality, and no sharper than a chosen DPI)
  - optionally run OCR to searchable on the result
  - settings are remembered
- The build runs in a separate process with "X of Y images" and Cancel; the window stays usable. Images are streamed (one at a time), so memory holds the PDF being built, not the whole photo set decoded.

## Compare (stage 13)
- **View › Compare Side by Side** (Ctrl+Alt+C) shows two PDFs. The DIFF row under it compares them. Choose **Text** (default) or **Visual**, set **Options…**, then **Compare**.
- **Text** compares word by word, using each word's own box on the page:
  - inserted words are marked on the right (green)
  - deleted words on the left (red)
  - changed words on both (amber)
  - the colours are in Settings › View / zoom
- Pages are matched by their content, not by number. An inserted, deleted or reordered page shows as "Page added", "Page removed" or "Page moved from left page N", and the pages after it don't light up as changed. A paragraph that reflowed onto the next page is matched there, so it is not a change.
- **Visual** marks the regions where the pages look different: drawings, scans, layout. Small anti-aliasing and sub-pixel differences are ignored; the threshold and smallest region are in Options.
- Options (remembered):
  - ignore whitespace, case, and punctuation-only changes
  - ignore running headers / footers and page numbers (the same rule as auto-bookmarks)
  - a page range on each side
  - text mode on a page without text (a scan) says so and suggests OCR to searchable or Visual mode
- The **Changes** panel lists every change in order (page, type, a snippet). Click one to jump both panes to it. **F7 / Shift+F7** go to the next / previous change, wrapping. Synced scrolling follows the page matching, and the summary counts everything.
- **Export report…** writes a NEW PDF: the summary, the change list, and side-by-side snippets of each change with its marks. It is never encrypted and never written over either PDF.
- The comparison runs in a separate process with progress and Cancel, so the window stays responsive. The marks are view-only: they never touch either document (no undo steps). Results clear when either document is edited; Compare runs it again.

## Default PDF reader (stage 12)
- **Settings › Default PDF reader**: "Make Lazarus the default PDF reader…" registers Lazarus for .pdf for your Windows account only (no admin rights). It writes its own ProgID `Lazarus.PDF` (name, icon, open command), an entry in "Open with", and the Capabilities that Settings › Default apps lists.
- Windows 10/11 does NOT let a program make itself the default. After registering, Lazarus opens the Default apps page, where you pick Lazarus for .pdf yourself, and it says so. Until then, double-clicked PDFs still go to the old default, and "Open with › Lazarus" works at once. The Settings section shows which of these states you're in.
- "Remove Lazarus's registration" takes out exactly what Lazarus added. It never touches another program's settings or .pdf's own default.
- **One Lazarus at a time.** A double-clicked PDF (or `Lazarus.exe file.pdf`) while Lazarus runs opens as a new tab in that window, which comes to the front. No second window starts.
  - If that PDF is already open, its tab is brought forward instead.
  - Several at once each become a tab, in order. They wait if the window is busy, e.g. building a find index.
  - Opening is view-only as always: the original is never written.
- Faster start: extraction's heavy libraries (OpenCV, NumPy, Tesseract) load only when extraction runs, and a newly opened PDF builds the Overview grid once. A big PDF still opens in the background with its progress showing.
- Thumbnails render only for what is visible plus a few each way, as you scroll, once each. While the PDF is as it was opened, a background thread renders them from its own copy of the file. Edited pages, and PDFs after page operations, render on the main thread a page at a time.

## Undo: journal steps and snapshot steps
- Undo/redo is one history with two step types (`workdoc.py`). Most edits are PyMuPDF **journal** steps (cheap).
  Ops PyMuPDF refuses inside a journal operation, or runs but can't undo correctly, are **snapshot** steps: the current document object stays alive, untouched, as the "before" state; the op runs on a fresh copy, which becomes the working document; undo/redo swap the two. Every view rebinds on a swap.
- Which type an op uses is **declared** in `oprules.py`, never decided by trying it and catching an error (a refused op can leave half-made changes; `set_toc` does). Each op was probed on worst-case files (no Info dictionary, a rotated page), and the self-test re-proves every classification.
  Today: annotations, text, images, redaction, rotation and flatten are journal ops; `set_metadata` (refused when a file has no Info dictionary yet) and `scrub` (undo doesn't restore) are snapshot ops. Page-structure ops keep their low-level MuPDF journal routes.
- Tools call `PdfEditorMode.run_edit(op, label, fn, pages)`; an op missing from `oprules.OPS` fails loudly.
- A new edit after undo drops the redo branch and closes the documents only it held. After undoing a snapshot step, the find index reuses that step's kept bytes instead of serializing again.
- **Settings › Undo memory**: snapshot steps may use up to N% of total RAM (default 30%), shared by all open PDFs (`memguard.py`). Free RAM is also checked before every snapshot step.
  When the budget or free RAM runs short, the oldest snapshot states become the undo floor (one notice per session). If an edit can't fit in free RAM even after that, it is refused and nothing changes; undo history isn't thrown away for an edit that can't happen anyway.

## Settings (File › Settings…, Ctrl+,)
Changes apply on OK or Apply. Restore Defaults refills the form, and nothing is saved until OK or Apply.
- **Output:** the default output folder, which is where extraction writes and what the import pop-up starts with. Also what Ctrl+S / Save does: save in place, or Save As… (pick a folder, then keep saving there). Review › Save in place and Save As… (Ctrl+Shift+S) are always available too.
- **Extraction / gaps:** the unfilled and filled gap highlight colours. They re-colour the open editor immediately, and the text colour adjusts to stay readable. Custom colours also apply to the written review HTML and PDF (the defaults keep the built-in look). The export page-headings toggle also lives here.
- **View / zoom:** the zoom a PDF opens at in the editor (Fit width, Fit page, or 50–200%).
- Settings live in `HKCU\Software\Lazarus\Lazarus`, the same QSettings store as before. Setting `LAZARUS_SETTINGS_INI=<file>` uses an INI file instead; tests and `--selftest` do this.
## Setup
- Tesseract 5.4.0 at `C:\Program Files\Tesseract-OCR` (UB-Mannheim build, eng + osd)
- A Python 3.11 venv (`.venv` in the source folder, or set `LAZARUS_VENV` / `LAZARUS_PY`): `python -m pip install -r requirements.txt`
- `lazarus/tess.py` finds Tesseract in this order: `LAZARUS_TESSERACT`, a bundled `tesseract\` folder, Program Files, LocalAppData, then PATH.

## Standalone .exe (onedir)
- One-time setup: `pip install -r requirements-build.txt` into the venv.
- `.\build.ps1` builds `dist\Lazarus\Lazarus.exe` (~376 MB folder). `dist\` and `build\` are gitignored.
- Installing is Setup's job (see below): the old copy-to-a-folder `-Install` switch is gone.
- `.\build.ps1 -SelfTest <test.pdf>` runs the frozen exe's self-test. You can also run `Lazarus.exe --selftest report.json test.pdf workdir` directly.
  - It runs offscreen and checks the bundled Tesseract, qtawesome fonts, the docx template, the icon, extraction with OCR, all six exports and all three modes.
- What the spec bundles:
  - Tesseract goes into `_internal\tesseract`, but only the 26 DLLs `tesseract.exe` actually imports (`tools/tess_deps.py`), plus eng + osd tessdata.
  - It drops OpenCV's ffmpeg DLL and Qt's Quick/QML/PDF/Network/OpenGL pieces, none of which Lazarus loads.
- Onedir rather than one-file: it starts quickly and runs Tesseract from a fixed folder.
  - A one-file exe would unpack ~376 MB to %TEMP% on every launch. That's slow, antivirus-unfriendly, and Tesseract would run out of a temp dir.

## Updates (stage 19b)
- At startup (File › Settings › Updates › "Check for updates at startup", on by default) Lazarus asks GitHub for the latest release of Crazy8697/Lazarus, in the background a few seconds after the window is up: the launch isn't delayed, nothing takes the focus, and any failure (offline, rate limit, a bad reply) stays silent. **Help › Check for updates…** checks by hand and always says how it went.
- A newer version shows a bar at the top of the window (not a pop-up): the version, **Release notes**, **Download and install**, **Skip this version** (remembered; a newer one still shows) and **Later**.
- Download and install: the installer is downloaded to %TEMP% with progress and Cancel and must match the release's published SHA-256 (`.sha256` asset); a missing or wrong checksum is refused. Unsaved work is asked about first (Save / Discard / Cancel; never saved by itself). Then Setup starts (one UAC prompt), Lazarus closes, the installation is upgraded in place and Lazarus starts again. A source run (venv, Lazarus.bat, the dev shortcut) checks but never installs.
- Privacy: the only network requests are GitHub's latest-release lookup (`/repos/Crazy8697/Lazarus/releases/latest`) and, if you choose to update, the installer and checksum download. No telemetry, no IDs, nothing about your files or settings.
- Dev / test hooks (never set in normal use): `LAZARUS_UPDATE_URL` points the check at another URL (a local mock server), `LAZARUS_UPDATE_TIMEOUT` sets the network timeout in seconds, `LAZARUS_UPDATE_DRYRUN=<file>` writes the Setup command line there instead of starting it, `LAZARUS_UPDATE_TEST_ACTION=install` answers the update bar with Download and install (scripted upgrade tests).

## Windows installer (stage 19)
- `.\build.ps1 -Setup` builds, then packages `dist\Lazarus` with Inno Setup 6 (`installer\lazarus.iss`) into `%USERPROFILE%\Lazarus-Setup\Lazarus-Setup-<version>.exe` plus its `.sha256` (outside the repo; never committed). `-Setup -NoBuild` packages the existing build; `-Setup -TestBuild` makes a "Lazarus Test" installer with its own AppId, folder and shortcut names, so tests never touch a real installation.
- `.\build.ps1 -Release -Version x.y.z` publishes: it refuses unless the git tree is clean, x.y.z is `lazarus.__version__`, `installer\release-notes\vx.y.z.md` exists and the tag isn't on GitHub yet; then it builds the Setup and `.sha256` and creates release vx.y.z on the public, releases-only repo **Crazy8697/Lazarus** (gh CLI) with both attached. Source code never goes to GitHub; it stays on Forgejo. Inno Setup is a build tool only (found through its own uninstall registration, or `LAZARUS_ISCC`); nothing of it ships.
- Setup installs to Program Files (64-bit) with one UAC prompt: a Start-menu shortcut, a desktop shortcut (checkbox, on by default), and an Apps / Add or Remove Programs entry (name, version, publisher, icon). The version is `lazarus.__version__`, the same one the About box shows.
- **Set Lazarus as my default PDF reader** (checkbox, off by default) runs the installed `Lazarus.exe --register-default` as the user who started Setup (unelevated, so it lands in that user's HKCU): the same per-user registration as Settings › Default PDF reader, then Settings › Default apps opens for the user to confirm. Nothing is made default silently.
- **Launch Lazarus** on the last page starts it unelevated.
- Uninstall removes the program files, the shortcuts and the Apps entry, and runs `Lazarus.exe --unregister-default`: the .pdf registration is removed exactly as Settings › Remove does, and only if it points at this install. User data stays: settings and the recent-files list (HKCU\Software\Lazarus\Lazarus), saved signatures (%APPDATA%\Lazarus\signatures), and every output folder.
- Updates from inside Lazarus (stage 19b) start this Setup with `/SILENT /SP- /SUPPRESSMSGBOXES /NORESTART /RELAUNCH=1`: the same AppId upgrades the installation in place (one UAC prompt; settings, shortcuts and the .pdf registration stay; the default-reader step isn't repeated) and `/RELAUNCH=1` starts Lazarus again afterwards as the original, unelevated user.
- Program Files is read-only for the program: nothing is written next to the exe (settings in the registry, signatures in %APPDATA%, temporary files in %TEMP%, outputs where the user chooses). The self-test checks it (`install_folder_untouched`). `tools/run_unelevated.py` runs a command de-elevated (the `runas /trustlevel:0x20000` token at Medium integrity, hidden) for testing an installed copy from an elevated shell.
## Extraction: Searchable PDF and Rebuilt pages (stage 21)
Both come from Extraction's **Export ▾** menu and use your CORRECTED review text: every word you changed and every gap you filled in. Both write a NEW PDF at a location you choose (the original, or any protected file, is refused), run in the background with progress and Cancel (a cancelled run writes nothing), and ask first if gaps are still open. They need the original PDF again: Lazarus remembers it from the import (and its password, in memory only); a review opened another way asks where the original is.
- **Searchable PDF…** (`-searchable.pdf`): the original pages exactly as they look, with your corrected text as an invisible, selectable, searchable layer behind them. Tesseract reads each page as displayed (the same engine and code as Document › OCR page, `lazarus/ocrlayer.py`), and your text is lined up with its words: an unchanged word keeps its exact position, a corrected word or filled gap sits where the word(s) it replaces are, text with nothing readable under it goes right after the word before it. Pages without a text layer (scans) get the corrected text for the whole page. Pages that already have a real text layer keep it untouched; only what Extraction had to OCR on them (pictures of text, the words the layer doesn't cover) gets your corrected text added. Open gaps carry no text. The output isn't encrypted.
- **Rebuilt pages…** (`-rebuilt.pdf`): a brand-new, clean PDF typeset from your corrected text, no scan images. Each original page becomes a page of the same size (more if its text runs over); 0.75 inch margins; 11 pt serif text (Windows' Times New Roman, embedded as a subset, else the built-in Times) at 1.38 line spacing; your paragraphs in order with a small gap between them, each original page starting a new page; the figures Extraction kept (pictures and drawings) centred where they came, at their printed size; open gaps show as their fill-in marker. The text is placed word by word, so it selects and searches exactly as typed.

## Privacy
Processed PDFs and outputs are never committed (see `.gitignore`). Never put a processed file's name, path, or contents in commits, logs, or notes.
