import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [workbookPath, previewPath] = process.argv.slice(2);
if (!workbookPath || !previewPath) {
  throw new Error("Usage: verify_workbook.mjs WORKBOOK PREVIEW.png");
}

const input = await FileBlob.load(workbookPath);
const workbook = await SpreadsheetFile.importXlsx(input);

const overview = await workbook.inspect({
  kind: "workbook,sheet",
  include: "id,name",
  maxChars: 4000,
});
const results = await workbook.inspect({
  kind: "table",
  range: "Results!A1:R30",
  tableMaxRows: 30,
  tableMaxCols: 18,
  tableMaxCellChars: 100,
  maxChars: 18000,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});

const preview = await workbook.render({
  sheetName: "Results",
  range: "A1:R25",
  scale: 1.2,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));

console.log(overview.ndjson);
console.log(results.ndjson);
console.log(errors.ndjson);
