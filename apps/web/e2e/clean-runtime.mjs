import { rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

// Playwright may load its config in multiple worker processes. Cleaning here,
// before either webServer starts, avoids deleting a live SQLite database.
rmSync(path.join(tmpdir(), "a-share-swing-quant-e2e"), {
  recursive: true,
  force: true,
});
