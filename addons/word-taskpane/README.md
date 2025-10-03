# Citation Verifier Word Task Pane (Prototype)

Prototype Office task pane that packages the current Word document as a base64 `.docx` and sends it to the existing `POST /api/verify` endpoint. The UI is intentionally minimal to maintain backend compatibility for both the Word add-in and the Next.js frontend.

## Layout

``` ...
│   
├── addons/
│   └── word-taskpane/
│      ├── manifest.dev.xml      # Sideload manifest for local testing
│      ├── package.json          # Vite + TypeScript setup, isolated from /app
│      ├── public/icon-*.png     # Placeholder icons referenced by the manifest
│      └── src/
│        ├── index.html          # Task pane markup
│        └── taskpane.ts         # Office.js logic that posts the base64 payload
   ...
```

## Prerequisites
- Node.js 18+
- Word desktop (Windows or macOS) with sideloading enabled
- Running backend (`uvicorn main:app --reload`) reachable at the URL passed via `VITE_BACKEND_URL`

## Local development
1. Install dependencies:
   ```bash
   cd addons/word-taskpane
   npm install
   ```
2. Configure the backend URL by creating `.env.local` (Vite loads it automatically):
   ```
   VITE_BACKEND_URL=https://your-backend-host:8000
   ```
   Defaults to `http://localhost:8000` if omitted.
3. Launch the HTTPS dev server (first run will prompts to trust the Vite certificate):
   ```bash
   npm run dev
   ```
   The task pane assets are served from `https://localhost:5174/`.
   If `ERR_EMPTY_RESPONSE` is returned, restart `npm run dev` and accept the self-signed certificate when prompted.

## Sideload in Word
1. **Prepare the shared-folder manifest (macOS build 16.68+):**
   ```bash
   mkdir -p ~/Library/Containers/com.microsoft.Word/Data/Documents/wef
   cp /Users/alexo/Projects/citation-verifier/addons/word-taskpane/manifest.dev.xml \
      ~/Library/Containers/com.microsoft.Word/Data/Documents/wef/citation-verifier.xml
   ```
   (On Windows, copy to `%LOCALAPPDATA%\Microsoft\Office\16.0\WEF\` instead.)
2. Start Word and open any document (preferably a copy; the add-in sends the full contents to the backend).
3. Insert → Add-ins → My Add-ins. Choose the **Shared Folder** tab (it appears only after Step 1). Select “Citation Verifier” to load the task pane.
4. If the add-in doesn’t appear, verify the manifest exists in the folder above and restart Word—cached manifests load when Word launches.

## Using the prototype
- Click **Verify Document**. The add-in gathers the document as compressed OOXML slices, stitches them into a base64 `.docx`, and posts it to `POST /api/verify`. The pane now surfaces a summary (totals, status counts, and up to five citation cards) alongside collapsible raw JSON and text preview.
- Status messages show progress (“Preparing document…”, “Contacting verifier…”, etc.).
- Errors (network, auth, backend validation) surface in the pane so for debugging without the Next.js UI.

## Next steps
- Add selection-only verification by building a temporary `.docx` from `getSelectedDataAsync` output.
- Stream progress back to the pane (SSE or polling) for large documents.
- Share styling/components with the Next.js app a shared UI package is formalized.

## License

This repository is publicly viewable for portfolio purposes only. The code is proprietary.
Copyright © 2025 Phaethon Order LLC. All rights reserved.
Contact [support@phaethon.llc](mailto:support@phaethon.llc) for licensing or reuse requests.

See [LICENSE](../../LICENSE)

## Contact
Questions or support: [support@phaethon.llc](mailto:support@phaethon.llc).
