// Arteries Pi extension scaffold.
// Add this extension to Pi, or copy the handler into your Pi extension bundle.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFileSync } from "node:child_process";

export default function arteries(pi: ExtensionAPI) {
  pi.on("session_before_compact", async (event) => {
    const result = execFileSync("bash", [".arteries/hooks/pi-compact-json.sh"], {
      input: JSON.stringify(event),
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
    });
    const packet = JSON.parse(result);
    return {
      compaction: {
        summary: packet.summary,
        firstKeptEntryId: event.preparation.firstKeptEntryId,
        tokensBefore: event.preparation.tokensBefore,
        details: packet.details,
      },
    };
  });
}
