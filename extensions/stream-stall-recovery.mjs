/**
 * Recover a transport-interrupted turn only when the committed output can
 * safely be continued. Derived from OMP's TurnRecovery semantics, implemented
 * using Pi 1.5's native agent_before_settle boundary API.
 */
const MAX_CONTINUATIONS = 3;
const MARKER = "stream-stall-recovery";
const RECONNECT_ERRORS = /\b(stream_incomplete|previous_response_owner_unavailable|previous_response_not_found|websocket|socket|stream (?:closed|ended|disconnected)|response\.completed|connection (?:closed|reset|lost)|fetch failed|terminated|network error|http\/2 stream)\b/i;

function hasVisibleText(message) {
  return Array.isArray(message?.content) && message.content.some((part) => part?.type === "text" && /\S/.test(part.text || ""));
}
function isTransientError(message) {
  return message?.role === "assistant" && message.stopReason === "error" && RECONNECT_ERRORS.test(message.errorMessage || "");
}
function isToolCall(block) { return block && block.type === "toolCall"; }
function isToolResult(message) { return message?.role === "toolResult"; }
function toolId(message) { return message?.toolCallId ?? message?.tool_call_id ?? message?.id; }

/** Pure evaluation, used by regression tests. */
export function evaluateRecovery(event) {
  if (event?.outcome !== "error" || !event?.context || event.context.pendingMessages?.length) return null;
  const messages = event.context.contextMessages || [];
  const lastUserIndex = messages.findLastIndex((m) => m?.role === "user");
  if (lastUserIndex < 0) return null;
  const recent = messages.slice(lastUserIndex + 1);
  const prior = recent.filter((m) => m?.role === "custom" && m.customType === MARKER).length;
  if (prior >= MAX_CONTINUATIONS) return null;
  let assistantIndex = -1;
  for (let i = recent.length - 1; i >= 0; i--) {
    if (recent[i]?.role === "assistant") { assistantIndex = i; break; }
  }
  if (assistantIndex < 0) return null;
  const current = recent[assistantIndex];
  if (!isTransientError(current)) return null;
  const later = recent.slice(assistantIndex + 1);
  // Only allow toolResult and our own recovery marker after a failed assistant.
  if (later.some((m) => !isToolResult(m))) return null;
  const blocks = current.content || [];
  if (!Array.isArray(blocks) || blocks.some((b) => b.type === "image" || b.type === "anthropicServerTool")) return null;
  const calls = blocks.filter(isToolCall);
  let kind;
  if (calls.length === 0) {
    if (later.length !== 0 || !hasVisibleText(current)) return null;
    kind = "text";
  } else {
    // Every issued call must already have exactly one recorded result. No more,
    // no less. Unknown tool status is always a hard stop, never a blind retry.
    const callIds = calls.map(toolId);
    const resultIds = later.map(toolId);
    if (new Set(callIds).size !== calls.length || callIds.some((id) => !id)) return null;
    if (new Set(resultIds).size !== later.length || later.length !== calls.length) return null;
    if (callIds.some((id) => !resultIds.includes(id))) return null;
    kind = "resolved-tools";
  }
  // Pi omits errored assistant turns from the next model projection, even when
  // the UI already displayed the prefix. Include that prefix explicitly so the
  // following request can continue without hallucinating the missing text.
  const emittedText = blocks.filter((b) => b.type === "text").map((b) => b.text || "").join("\n");
  const retainedText = emittedText.slice(-12000);
  const prompt = kind === "text"
    ? "The assistant's last response was interrupted after emitting the following text. The existing text was already shown; continue immediately after its final words WITHOUT repeating it or running previous tools.\n<previous_assistant_text>\n" + retainedText + "\n</previous_assistant_text>"
    : "The previous assistant response was interrupted, but its tool calls already have recorded results. Continue using those results. Never rerun the completed tools or repeat side effects.";
  return { kind, attempt: prior + 1, entries: [{ type: "custom_message", customType: MARKER, content: prompt, display: false }], continue: true };
}

export default function streamStallRecovery(pi) {
  pi.on("agent_before_settle", (event) => evaluateRecovery(event));
}