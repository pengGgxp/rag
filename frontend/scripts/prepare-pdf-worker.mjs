import { copyFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
const publicPath = new URL("../public/", import.meta.url);
mkdirSync(publicPath, { recursive: true });
copyFileSync(fileURLToPath(new URL("../node_modules/pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url)), fileURLToPath(new URL("pdf.worker.min.mjs", publicPath)));
