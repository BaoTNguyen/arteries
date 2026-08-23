// Arteries Pi extension. Add to Pi, or copy the handlers into your bundle.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFileSync } from "node:child_process";

function run(args: string[], input?: unknown, extraEnv: Record<string, string> = {}): string {
  try {
    return execFileSync("bash", args, {
      input: input === undefined ? undefined : JSON.stringify(input),
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
      env: { ...process.env, ARTERIES_CLI: "pi", ...extraEnv },
    });
  } catch (err) {
    return ""; // memory is never allowed to break a turn
  }
}

// Pi hands us the counts directly on the assistant message, so there is no
// transcript to parse — pass them through the ARTERIES_USAGE_* contract that
// exists for exactly this case. Pi bills cache writes as one bucket; arteries
// splits 5m/1h, and 5m is both the common case and the cheaper of the two.
function usageEnv(usage: any, model?: string): Record<string, string> {
  if (!usage) return {};
  const env: Record<string, string> = {};
  const put = (k: string, v: unknown) => {
    if (typeof v === "number" && v > 0) env[k] = String(Math.round(v));
  };
  put("ARTERIES_USAGE_TOKENS_IN", usage.input);
  put("ARTERIES_USAGE_TOKENS_OUT", usage.output);
  put("ARTERIES_USAGE_CACHE_READ", usage.cacheRead);
  put("ARTERIES_USAGE_CACHE_WRITE_5M", usage.cacheWrite);
  if (model && Object.keys(env).length) env.ARTERIES_USAGE_MODEL = model;
  return env;
}

function messageText(message: any): string {
  const content = message?.content;
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content
    .filter((part: any) => part?.type === "text" && typeof part.text === "string")
    .map((part: any) => part.text)
    .join("\n")
    .trim();
}

export default function arteries(pi: ExtensionAPI) {
  // Fired after the user submits, before the agent loop — the prompt hook.
  // Returning a custom message is how context reaches the model in Pi; there is
  // no stdout convention like Claude's.
  pi.on("before_agent_start", async (event) => {
    const prompt = (event?.prompt ?? "").trim();
    if (!prompt) return;
    const context = run(["/home/bao-tn/Coding/Projects/arteries/.arteries/hooks/hook-observe.sh", prompt], event).trim();
    if (!context) return;
    return {
      message: {
        customType: "arteries-memory",
        content: context,
        display: false,
      },
    };
  });

  // message_end is the finalized assistant message: text complete, usage
  // populated. message_update fires per token and would re-report every chunk.
  pi.on("message_end", async (event) => {
    const message = event?.message;
    if (message?.role !== "assistant" || !messageText(message)) return;
    run(
      ["/home/bao-tn/Coding/Projects/arteries/.arteries/hooks/hook-assistant-observe.sh", "pi-assistant"],
      event,
      usageEnv(message.usage, message.responseModel ?? message.model),
    );
  });

  pi.on("session_before_compact", async (event) => {
    const result = run(["/home/bao-tn/Coding/Projects/arteries/.arteries/hooks/pi-compact-json.sh"], event);
    if (!result) return;
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
