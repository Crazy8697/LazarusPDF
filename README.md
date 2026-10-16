<p align="center">
  <img src="assets/brand/darkrelay-banner.jpg" alt="DarkRelay" width="100%">
</p>

<h1 align="center">Lazarus PDF</h1>

<p align="center"><b>A free, local PDF editor and reader for Windows.</b><br>
Read, mark up, edit, rearrange, fill in, protect and export PDFs. Everything stays on your computer.</p>

## Download

<p align="center">
  <a href="https://github.com/Crazy8697/LazarusPDF/releases/latest/download/LazarusPDF-Setup.exe">
    <img src="https://img.shields.io/badge/Download%20Lazarus%20PDF%20for%20Windows-0a84ff?style=for-the-badge&logo=windows&logoColor=white" alt="Download Lazarus PDF for Windows" height="56">
  </a>
</p>

<!-- dl-info -->
<p align="center"><sub>Windows 10 / 11, 64-bit &nbsp;&middot;&nbsp; version 0.22.1 &nbsp;&middot;&nbsp; 104 MB &nbsp;&middot;&nbsp; <a href="https://github.com/Crazy8697/LazarusPDF/releases">all releases</a></sub></p>
<!-- /dl-info -->

<p align="center"><sub>The installer isn't code-signed, so Windows may show "Windows protected your PC": click <b>More info</b>, then <b>Run anyway</b>.</sub></p>

Run the downloaded `LazarusPDF-Setup.exe`: it installs Lazarus PDF to Program Files, adds Start-menu (and
optionally desktop) shortcuts, and can register Lazarus PDF as your PDF reader (Windows then asks you to
confirm it in Settings › Default apps). Each release also lists the versioned installer and its
`.sha256` checksum. Uninstall from Windows Settings › Apps; your settings and saved signatures stay.

## What Lazarus PDF does

- **Read and navigate:** tabs, thumbnails, Overview grid, bookmarks (also generated from headings),
  find, side-by-side compare with the differences marked.
- **Mark up:** highlight, underline, strikeout, draw, shapes, notes, text boxes, stamps, callouts,
  measurements, signatures, images, links, and an Annotations panel listing them all.
- **Edit:** change existing text in place, find and replace, whiteout, true redaction (verified).
- **Pages:** reorder, rotate, delete, insert, duplicate, crop, extract, merge, split, and copy or drag
  pages between open PDFs.
- **Documents:** fill in and create forms, headers / footers, page numbers, Bates numbers, watermarks,
  metadata, flatten, compress, passwords and permissions (AES-256), OCR to searchable PDF.
- **Create and convert:** PDFs from images or your scanner; export to Word, EPUB, Markdown, HTML, text
  or PDF; batch processing of many files.
- **Extraction mode:** pull the text out of a PDF (text layer, else OCR), see every unreadable gap
  highlighted for you to fill in, then turn your corrected text into a searchable PDF (the original
  pages with your text as an invisible, searchable layer) or clean rebuilt pages (a new typeset PDF).
- Every change can be undone, and an original PDF is never written: saves and exports always go to
  a new file.

The [manual](docs/MANUAL.md) describes every feature in detail.

## Privacy

Lazarus PDF works entirely on your computer and writes no logs. Its only network requests are:

- at startup, one lookup of the latest release on this GitHub repository (see below), and
- if you choose to update, the download of the new installer and its checksum.

No telemetry, no IDs, nothing about your files or settings is sent anywhere.

## Updates

A few seconds after it starts, Lazarus PDF checks this repository's latest release in the background
and shows a bar at the top of the window when a newer version is out: **Download and install**,
**Skip this version** or **Later**. The download is checked against the published SHA-256 before it
runs, and your open work is never closed without asking. Turn the check off in
**File › Settings › Updates › "Check for updates at startup"**; **Help › Check for updates…** checks
by hand.

## Build from source

Requirements: Windows 10 / 11 64-bit, Python 3.11, and (for the installer) Inno Setup 6.

```powershell
git clone https://github.com/Crazy8697/LazarusPDF.git
cd LazarusPDF
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.\LazarusPDF.bat                                 # run from source
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1 -Setup                               # dist\LazarusPDF + %USERPROFILE%\LazarusPDF-Setup\LazarusPDF-Setup-<version>.exe
```

The build bundles Tesseract OCR: install the [UB Mannheim build of Tesseract 5](https://github.com/UB-Mannheim/tesseract/wiki)
(English + OSD) to `C:\Program Files\Tesseract-OCR` before building. `LazarusPDF.exe --selftest REPORT.json TEST.pdf WORKDIR`
runs the built-in self-test. More in [docs/MANUAL.md](docs/MANUAL.md).

## License

Lazarus PDF is free software: you can redistribute it and/or modify it under the terms of the
[GNU Affero General Public License v3.0](LICENSE). It comes with NO WARRANTY. It includes third-party
components under their own licenses: see [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt)
(also in the program under **Help › Licenses**).

## Support

Lazarus PDF is made by DarkRelay. If it's useful to you, you can
[buy DarkRelay a coffee](https://buymeacoffee.com/crazy8697).
